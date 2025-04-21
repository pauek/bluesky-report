import click, asyncio, requests_cache
from datetime import datetime
from dataclasses import dataclass
from typing import Any, TypeAlias
from dataclasses import dataclass
from aiohttp_client_cache import CachedSession, SQLiteBackend

# Constants #######################################################################################

API_HOST = "https://public.api.bsky.app"
GET_PROFILES_MAX_ACTORS = 25
GET_RELATIONSHIPS_MAX_OTHERS = 30
GET_FEED_REQ_LIMIT = 50
MAX_REQUESTS_IN_FLIGHT = 50


# Calling the API #################################################################################

ParamValue: TypeAlias = str | int | list[str] | list[int]
Params: TypeAlias = dict[str, ParamValue]

# Global session to speed up non-async calls
session = requests_cache.CachedSession("bsky")


def __call_api(nsid: str, params: Params) -> Any:
    # print(f"{nsid}: {params}")
    response = session.get(f"{API_HOST}/xrpc/{nsid}", params=params)
    if not response.ok:
        print("URL:    ", response.url)
        print("Code:   ", response.status_code)
        print("Reason: ", response.reason)
        print("Text:   ", response.text)
        return "{}"  # Valid JSON...
    return response.json()


async def __call_api_async(session, nsid: str, params: Params) -> Any:
    # print(f"__call_api_async('{nsid}', {params})")
    async with session.get(f"{API_HOST}/xrpc/{nsid}", params=params) as response:
        if not response.ok:
            print("URL:    ", response.url)
            print("Code:   ", response.status_code)
            print("Reason: ", response.reason)
            print("Text:   ", response.text)
            return "{}"  # Valid JSON...
        return await response.json()


# Data types


@dataclass
class Profile:
    did: str
    handle: str
    displayName: str
    createdAt: datetime
    description: str
    followersCount: int
    followsCount: int

    @staticmethod
    def fromJson(json: Any):
        return Profile(
            json["did"],
            json["handle"],
            json["displayName"] if "displayName" in json else "",
            json["createdAt"],
            json["description"] if "description" in json else "",
            json["followersCount"] if "followersCount" in json else 0,
            json["followsCount"] if "followsCount" in json else 0,
        )


@dataclass
class Post:
    uri: str
    author: Profile
    createdAt: datetime
    text: str
    replyCount: int
    repostCount: int
    likeCount: int
    quoteCount: int

    @staticmethod
    def fromJson(json: Any):
        return Post(
            json["uri"],
            Profile.fromJson(json["author"]),
            json["record"]["createdAt"],
            json["record"]["text"],
            json["replyCount"],
            json["repostCount"],
            json["likeCount"],
            json["quoteCount"],
        )


@dataclass
class Repost:
    post: Post
    by: str


@dataclass
class Thread:
    post: Post
    replies: list["Thread"]

    @staticmethod
    def fromJson(json: Any):
        replies = json["replies"] if "replies" in json else []
        return Thread(
            Post.fromJson(json["post"]),
            [Thread.fromJson(r) for r in replies],
        )


# Bluesky calls ###################################################################################


def get_follower_handles(handle: str) -> list[str]:
    r"""Get the handles of the followers of a user.

    :param atid: The user's "handle" (e.g. "`fchollet.bsky.social`")

    :returns handles: `list[str]`
        A list of the handles of the followers of user with `handle`

    """
    follower_handles: list[str] = []
    result = __call_api(
        "app.bsky.graph.getFollowers",
        {"actor": handle, "limit": 100},
    )
    cursor = result["cursor"] if "cursor" in result else None
    while True:
        fhandles = result["followers"] if "followers" in result else []
        follower_handles += [f["handle"] for f in fhandles]
        if not cursor:
            break
        result = __call_api(
            "app.bsky.graph.getFollowers",
            {"actor": handle, "limit": 100, "cursor": cursor},
        )
        cursor = result["cursor"] if "cursor" in result else None

    return follower_handles


def get_followers(handle: str) -> list[Profile]:
    return get_profiles(get_follower_handles(handle))


def get_feed(handle: str, limit: int = 20) -> list[Post | Repost]:
    r"""Return a limited list of Posts from a user.

    :param handle: The handle of the user (e.g. "`fchollet.bsky.social`")
    :param limit: The maximum number of posts to return (default: 20)

    :returns feed: The list of `Post` or `Repost` objects
    """
    num_posts: int = min(limit, GET_FEED_REQ_LIMIT)
    feed: list[Any] = []
    data = __call_api(
        "app.bsky.feed.getAuthorFeed", {"actor": handle, "limit": num_posts}
    )
    cursor = data["cursor"] if "cursor" in data else None
    while len(feed) < limit:
        feed += data["feed"] if "feed" in data else []
        if not cursor:
            break
        data = __call_api(
            "app.bsky.feed.getAuthorFeed",
            {"actor": handle, "limit": num_posts, "cursor": cursor},
        )
        cursor = data["cursor"] if "cursor" in data else None

    result: list[Post | Repost] = []
    for f in feed:
        post = Post.fromJson(f["post"])
        if "reason" in f:
            if f["reason"]["$type"] == "app.bsky.feed.defs#reasonRepost":
                post = Repost(post, f["reason"]["by"]["handle"])
            else:
                print("Something else?", f["reason"]["$type"])
        result.append(post)
    return result


def __chunked(seq, size):
    return (seq[pos : pos + size] for pos in range(0, len(seq), size))


def get_profiles(handles: list[str]) -> list[Profile]:
    """Get the profiles of users from their IDs.

    :param ids: A list of the IDs (bsky handles) for the wanted users

    :returns profiles: A list of `Profile` objects
    """
    unique_handles = [h for h in set(handles)]
    if not unique_handles:
        return []

    async def __get_profiles(groups: list[list[str]]) -> list[Profile]:
        profiles: list[Profile] = []
        async with CachedSession(cache=SQLiteBackend("bsky_cache")) as session:
            for request_chunk in __chunked(groups, MAX_REQUESTS_IN_FLIGHT):
                tasks = [
                    asyncio.ensure_future(
                        __call_api_async(
                            session, "app.bsky.actor.getProfiles", {"actors": req}
                        )
                    )
                    for req in request_chunk
                ]
                results = await asyncio.gather(*tasks)
                for json in results:
                    if json == None:
                        continue
                    if "profiles" in json:
                        profiles += [Profile.fromJson(p) for p in json["profiles"]]
        return profiles

    # Groups of max GET_PROFILES_MAX_ACTORS
    groups = [chunk for chunk in __chunked(unique_handles, GET_PROFILES_MAX_ACTORS)]
    return asyncio.run(__get_profiles(groups))


def get_thread(uri: str) -> Thread:
    """Get a post and its replies as a recursive data structure

    :param uri: The post URI as is returned by the function `get_feed`
                (e.g. `at://did:plc:<user-did>/app.bsky.feed.post/<post-id>`)

    :returns thread: A `Thread` object (possibly containing more `Thread` objects
        in the `replies` field)
    """
    result = __call_api("app.bsky.feed.getPostThread", {"uri": uri, "depth": 100})
    return Thread.fromJson(result["thread"])


class Relationships:
    following: list[str]
    followedBy: list[str]

    def __init__(self):
        self.following = []
        self.followedBy = []


def get_relationships(did: str, others_dids: list[str]) -> Relationships:

    async def __get_relationships(groups: list[list[str]]) -> Relationships:
        relationships = Relationships()
        async with CachedSession(cache=SQLiteBackend("bsky_cache")) as session:
            for requests in __chunked(groups, MAX_REQUESTS_IN_FLIGHT):
                tasks = [
                    asyncio.ensure_future(
                        __call_api_async(
                            session,
                            "app.bsky.graph.getRelationships",
                            {"actor": did, "others": others},
                        )
                    )
                    for others in requests
                ]
                results = await asyncio.gather(*tasks)
                for json in results:
                    if "relationships" in json:
                        for rel in json["relationships"]:
                            if "following" in rel:
                                relationships.following.append(rel["did"])
                            if "followedBy" in rel:
                                relationships.followedBy.append(rel["did"])
        return relationships

    groups = [chunk for chunk in __chunked(others_dids, GET_RELATIONSHIPS_MAX_OTHERS)]
    return asyncio.run(__get_relationships(groups))


# Command line interface to test bsky operations ##################################################


@click.group()
def main():
    pass


@main.command("profile")
@click.argument("handle")
def cmd_get_profile(handle: str):
    [profile] = get_profiles([handle])
    print(profile)


@main.command("followers")
@click.argument("handle")
def cmd_get_profiles(handle):
    followers = get_followers(handle)
    profiles = get_profiles([f.did for f in followers])
    for p in profiles:
        print(p.did, p.handle, f'"{p.displayName}"', p.followersCount, p.followsCount)


@main.command("relationships")
@click.argument("handle")
def cmd_get_relationships(handle):
    [root] = get_profiles([handle])
    print(root)

    followers = get_followers(handle)

    did2profile: dict[str, Profile] = {root.did: root}
    for profile in followers:
        did2profile[profile.did] = profile

    relationships = get_relationships(root.did, [f.did for f in followers])

    print(f"--- {did2profile[root.did].displayName} ---")
    print("Followed By:")
    for did in relationships.followedBy:
        print(f"  {did2profile[did].displayName}")
    print("Following:")
    for did in relationships.following:
        print(f"  {did2profile[did].displayName}")


@main.command("feed")
@click.argument("handle")
def cmd_get_feed(handle):
    feed = get_feed(handle)
    for f in feed:
        if isinstance(f, Post):
            print(f"🖋️  Post({f.uri})")
        elif isinstance(f, Repost):
            print(f"🗘  Repost[{f.post.author.handle}]({f.post.uri})")


@main.command("thread")
@click.argument("thread_uri")
def cmd_get_thread(thread_uri):
    def print_thread(thread: Thread, level: int = 0):
        handle = thread.post.author.handle
        text = thread.post.text
        print(f"{"    " * level}{handle}: {text}")
        for reply in thread.replies:
            print_thread(reply, level + 1)

    print_thread(get_thread(thread_uri))


if __name__ == "__main__":
    main()
