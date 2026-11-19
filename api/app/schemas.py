from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ImageDTO(BaseModel):
    model_config = ConfigDict(extra="allow")

    url: str
    width: int
    height: int
    thumb: str | None = None
    spoiler: bool | None = None
    size: int | None = None
    original_url: str | None = None
    original_size: int | None = None
    original_name: str | None = None


class PostReactionDTO(BaseModel):
    emoji: str | None = None
    custom_emoji_id: str | None = None
    count: int


class PostReplyDTO(BaseModel):
    id: int
    text: str | None = None
    thumb: str | None = None


class PostQuoteDTO(BaseModel):
    text: str
    position: int | None = None


class PostDTO(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    date: str
    text: str | None = None
    forwarded_from: str | dict[str, Any] | None = None
    type: str | None = None
    views: str | None = None
    author: str | None = None
    reply: PostReplyDTO | None = None
    images: list[ImageDTO] | None = None
    files: list[dict[str, Any]] | None = None
    quote: PostQuoteDTO | None = None
    reactions: list[PostReactionDTO] | None = None
    show_caption_above_media: bool | None = None
    has_media_spoiler: bool | None = None


class PostsListResponse(BaseModel):
    posts: list[dict[str, Any]]


class ExportMetaDTO(BaseModel):
    total_posts: int
    page_size: int
    latest_id: int | None
    updated_at: str


class MergedReactionDTO(BaseModel):
    reaction_key: str
    emoji: str | None = None
    custom_emoji_id: str | None = None
    tg_count: int
    web_count: int
    count: int
    reacted: bool = False


class MergedPollOptionDTO(BaseModel):
    idx: int
    text: str
    tg_voter_count: int
    web_voter_count: int
    voter_count: int


class MergedPollDTO(BaseModel):
    id: str
    question: str
    is_closed: bool
    is_anonymous: bool
    type: str
    allow_multiple_answers: bool
    total_voter_count: int
    my_votes: list[int]
    correct_option_idx: int | None = None
    explanation: str | None = None
    options: list[MergedPollOptionDTO]


class HealthResponse(BaseModel):
    status: str
    database: str


class UserDTO(BaseModel):
    id: int
    display_name: str
    role: str
    avatar_url: str | None = None
    provider: str
    email_verified: bool
    notify_replies: bool


class AuthMeResponse(BaseModel):
    user: UserDTO | None
    providers: list[str]
    allow_anon_comments: bool
    telegram_bot_username: str | None = None


class UpdateMeRequest(BaseModel):
    display_name: str | None = None
    notify_replies: bool | None = None


class UpdateMeResponse(BaseModel):
    user: UserDTO


class OkResponse(BaseModel):
    ok: bool = True


class TelegramAuthPayload(BaseModel):
    id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None
    auth_date: int
    hash: str


class MagicLinkRequest(BaseModel):
    email: str
    display_name: str | None = None
    altcha_payload: str
    next: str | None = None


class AltchaChallengeDTO(BaseModel):
    algorithm: str
    challenge: str
    maxnumber: int
    salt: str
    signature: str


class PostInteractionDTO(BaseModel):
    reactions: list[MergedReactionDTO]
    poll: MergedPollDTO | None = None
    comment_count: int


class InteractionsResponse(BaseModel):
    allow_anon_comments: bool
    captcha_provider: str = "altcha"
    me: UserDTO | None = None
    posts: dict[str, PostInteractionDTO]


class ReactionToggleRequest(BaseModel):
    emoji: str | None = None
    custom_emoji_id: str | None = None


class ReactionToggleResponse(BaseModel):
    post_id: int
    reactions: list[MergedReactionDTO]


class PollVoteRequest(BaseModel):
    option_indices: list[int]


class PollVoteResponse(BaseModel):
    post_id: int
    poll: MergedPollDTO


class CommentAuthorDTO(BaseModel):
    display_name: str
    avatar_url: str | None = None
    role: str
    provider: str | None = None
    username: str | None = None
    verified: bool


class CommentDTO(BaseModel):
    id: int
    post_id: int
    parent_id: int | None = None
    source: str
    author: CommentAuthorDTO
    body_text: str
    body_html: str
    media: Any | None = None
    created_at: str
    edited_at: str | None = None
    deleted: bool = False
    is_mine: bool = False
    can_delete: bool = False


class CommentsListResponse(BaseModel):
    post_id: int
    comments: list[CommentDTO]


class CreateCommentRequest(BaseModel):
    body: str
    parent_id: int | None = None
    as_anonymous: bool = False
    altcha_payload: str | None = None


class DeleteCommentResponse(BaseModel):
    ok: bool = True
    post_id: int
    comment_id: int
