<template>
    <TgBlog
        :postsUrl="life.postsUrl"
        :postsData="postsData"
        :apiUrl="activeApiUrl || undefined"
        :telegramBotUsername="life.telegramBotUsername || undefined"
        :sharedId="sharedPostId"
        :messages="lifeMessages"
        :anonAvatars="anonAvatars"
    >
        <template #default="{ isOwner, pendingReviewCount, openModeration }">
            <div class="title">
                <div class="title-row">
                    <h2>生活</h2>
                    <div class="title-actions">
                        <button
                            v-if="isOwner"
                            type="button"
                            class="rss-link mod-queue-btn clickable"
                            :class="{ 'has-pending': pendingReviewCount > 0 }"
                            @click="openModeration()"
                        >
                            <i class="fas fa-shield-halved" aria-hidden="true"></i>
                            <span>{{ lifeMessages.reviewQueueBtn }}</span>
                            <span v-if="pendingReviewCount > 0" class="mod-pending-count">{{ pendingReviewCount }}</span>
                        </button>
                        <a
                            v-if="life.rssUrl"
                            class="rss-link"
                            :class="{copied: rssCopied}"
                            :href="life.rssUrl"
                            target="_blank"
                            rel="noopener noreferrer"
                            :title="rssCopied ? '已复制 RSS 链接' : '点击复制 RSS 订阅链接'"
                            @click.prevent="onRssClick"
                        >
                            <i :class="rssCopied ? 'fas fa-check' : 'fas fa-rss'" aria-hidden="true"></i>
                            <span>{{ rssCopied ? '已复制 RSS 链接' : 'RSS' }}</span>
                        </a>
                    </div>
                </div>
            <div class="subtitle">
                <p>
                    在这个时间只会正向流动的世界里，每当秒针划过刻度线，就不可能再回到那里了——每一刻都是我们的第一次、也是最后一次。二十岁，人生中最有趣的这段时光，只匆匆忙忙地经历一次太可惜了。但是或许可以通过这里的照片和文字，让未来的自己能够重新体验这些心情起伏——这是这个频道的初衷。
                </p>
                <p>
                    这里是我记录生活的地方，没有固定格式和内容，更新也会比较频繁。
                </p>
            </div>
        </div>
        </template>
    </TgBlog>
</template>

<script lang="ts" setup>
import {computed, onMounted, onUnmounted, ref, watch} from 'vue'
import {TgBlog, zhCnMessages} from 'tg-blog'
import type {Post, TgBlogMessages} from 'tg-blog'
import 'tg-blog/dist/style.css'
import {life} from '@/scripts/constants'

definePageMeta({title: '生活', navBookmark: 'life'})

const anonAvatars = Array.from({length: 9}, (_, idx) => `/anons/anon-${idx + 1}.webp`)

const lifeMessages: TgBlogMessages = {
    ...zhCnMessages,
    searchPlaceholder: '搜索动态、标签或日期...',
    loadFailed: '加载动态失败：',
    forwardedFrom: '转发自：',
    replyTo: '回复：',
    voiceMessage: '语音消息',
    copyShareLink: '复制分享链接',
    linkCopied: '链接已复制',
    poll: '投票',
    quiz: '问答',
    anonymousPrefix: '匿名',
    pollResults: (count) => `共 ${count} 人参与`,
    photoIndex: (index, total) => `第 ${index} / ${total} 张`,
    dateFormat: 'YYYY-MM-DD HH:mm',

    // Interactive: Reactions
    addReaction: '添加回应',

    // Interactive: Polls
    pollVote: '提交投票',
    pollRetractVote: '撤回投票',
    pollClosed: '已结束',
    pollExplanation: '解析',
    pollMultipleChoice: '可多选',

    // Interactive: Chat / Comments
    reply: '讨论',
    commentsCount: (count) => `${count}`,
    noComments: '还没有消息，来聊两句吧～',
    loadingComments: '加载消息中...',
    commentsLoadFailed: '消息加载失败',
    retry: '重试',
    writeCommentPlaceholder: '发消息...',
    replyingTo: (name) => `回复 @${name}`,
    cancelReply: '取消',
    postComment: '发送',
    postingComment: '发送中...',
    deleteComment: '删除',
    confirmDelete: '确认删除？',
    commentDeleted: '该消息已撤回',
    signInToComment: '登录后即可参与讨论',
    signIn: '登录',
    signOut: '退出登录',
    commentAsAnonymous: '匿名发送',
    commentingAnonymously: '匿名模式',
    notifyRepliesOn: '回复邮件提醒：开',
    notifyRepliesOff: '回复邮件提醒：关',

    // Interactive: Badges
    badgeOwner: '站长',
    badgeVerified: '已验证',
    badgeTelegram: 'Telegram',
    badgeAnonymous: '匿名',

    // Interactive: ALTCHA PoW
    altchaPrompt: '点击进行人机验证',
    altchaVerifying: '验证中...',
    altchaVerified: '已通过验证',
    altchaError: '验证失败，点击重试',

    // Interactive: Auth Modal
    authTitle: '登录',
    authSubtitle: '登录后可获得已验证标识，并接收讨论回复的邮件提醒。',
    authContinueWithGithub: '使用 GitHub 登录',
    authContinueWithGoogle: '使用 Google 登录',
    authContinueWithTelegram: '使用 Telegram 登录',
    authContinueWithOidc: (providerName) => `使用 ${providerName} 登录`,
    authOrEmail: '或使用邮箱免密登录',
    authEmailPlaceholder: '输入你的邮箱地址',
    authDisplayNamePlaceholder: '昵称（可选）',
    authSendMagicLink: '发送登录链接',
    authSendingMagicLink: '发送中...',
    authMagicLinkSent: (email) => `登录链接已发送至 ${email}，请查收邮件完成登录。`,
    authClose: '关闭',

    // Original Photo Viewer
    viewOriginal: '查看原图',
    viewOriginalWithSize: (size) => `查看原图 (${size})`,
    loadingOriginal: '原图加载中...',
    viewingOriginal: '正在查看原图',
}

useHead(() => {
    const link: Array<{rel: 'alternate'; type: string; title: string; href: string}> = []
    if (life.rssUrl) {
        link.push({
            rel: 'alternate',
            type: 'application/rss+xml',
            title: 'Aza 的生活 - RSS',
            href: life.rssUrl,
        })
    }
    if (life.atomUrl) {
        link.push({
            rel: 'alternate',
            type: 'application/atom+xml',
            title: 'Aza 的生活 - Atom',
            href: life.atomUrl,
        })
    }
    if (life.jsonFeedUrl) {
        link.push({
            rel: 'alternate',
            type: 'application/feed+json',
            title: 'Aza 的生活 - JSON Feed',
            href: life.jsonFeedUrl,
        })
    }
    return {link}
})

const route = useRoute()

const rssCopied = ref(false)
let rssCopiedTimer: ReturnType<typeof setTimeout> | null = null

function copyViaTextarea(text: string): boolean {
    if (typeof document === 'undefined' || !document.body) return false
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.left = '-9999px'
    ta.style.top = '0'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    try {
        ta.focus({preventScroll: true})
        ta.select()
        ta.setSelectionRange(0, text.length)
        return document.execCommand('copy')
    } catch {
        return false
    } finally {
        document.body.removeChild(ta)
    }
}

async function onRssClick() {
    if (!import.meta.client || !life.rssUrl) return
    const absUrl = new URL(life.rssUrl, window.location.origin).toString()

    let copied = false
    if (navigator.clipboard?.writeText) {
        try {
            await navigator.clipboard.writeText(absUrl)
            copied = true
        } catch {
            copied = false
        }
    }
    if (!copied) {
        copied = copyViaTextarea(absUrl)
    }

    if (copied) {
        rssCopied.value = true
        if (rssCopiedTimer) clearTimeout(rssCopiedTimer)
        rssCopiedTimer = setTimeout(() => {
            rssCopied.value = false
            rssCopiedTimer = null
        }, 2000)
    } else {
        window.open(absUrl, '_blank', 'noopener,noreferrer')
    }
}

const sharedPostId = computed<number | null>(() => {
    const raw = route.query.shared
    const str = typeof raw === 'string' ? raw : Array.isArray(raw) ? raw[0] : null
    if (!str) return null
    const parsed = Number(str)
    return Number.isFinite(parsed) ? parsed : null
})

const {data: previewPosts} = await useFetch<Post[]>('/api/life-preview.json', {
    key: 'life-preview',
    default: () => [],
})

const postsData = ref<Post[] | undefined>(
    previewPosts.value && previewPosts.value.length > 0 ? previewPosts.value : undefined
)
const activeApiUrl = ref<string>(life.apiUrl)

const LOCAL_OVERRIDE_HOSTS = new Set(['127.0.0.1', 'localhost', '::1', '[::1]'])

// Allow localhost-only same-origin API override (?lifeApi=/api/life) for local E2E testing
// of interactive mode without exposing any override vector in production.
function syncLocalApiOverride() {
    if (!import.meta.client) return
    if (!LOCAL_OVERRIDE_HOSTS.has(window.location.hostname)) return
    const raw = route.query.lifeApi
    const override = typeof raw === 'string' ? raw : Array.isArray(raw) ? raw[0] : null
    if (
        override
        && /^\/[a-zA-Z0-9/_-]+$/.test(override)
        && !override.includes('//')
        && !override.includes('..')
    ) {
        const normalized = override.replace(/\/+$/, '')
        activeApiUrl.value = normalized || life.apiUrl
    } else {
        activeApiUrl.value = life.apiUrl
    }
}

watch(() => route.query.lifeApi, syncLocalApiOverride)

let archiveAbortController: AbortController | null = null

onMounted(async () => {
    syncLocalApiOverride()

    // When SSR/preview seeded the newest 20 posts, fetch the full archive in the
    // background so infinite scroll, search, and older ?shared= links have all posts.
    if (!postsData.value) return
    archiveAbortController = new AbortController()
    try {
        const res = await fetch(life.postsUrl, {signal: archiveAbortController.signal})
        if (!res.ok) return
        const fullPosts = await res.json() as Post[]
        if (Array.isArray(fullPosts) && fullPosts.length > 0) {
            postsData.value = fullPosts
        }
    } catch (err) {
        if ((err as {name?: string})?.name === 'AbortError') return
        console.warn('[life] Failed to hydrate full posts archive:', err)
    }
})

onUnmounted(() => {
    if (rssCopiedTimer) {
        clearTimeout(rssCopiedTimer)
        rssCopiedTimer = null
    }
    archiveAbortController?.abort()
    archiveAbortController = null
})
</script>

<style lang="sass" scoped>
@use "../css/colors"
@use "../css/responsive"

.title
    text-align: left
    margin-bottom: 30px

.title-row
    display: flex
    align-items: center
    justify-content: space-between
    gap: 12px
    margin: 0.83em 0 0

    h2
        margin: 0
        line-height: 1.2

.title-actions
    display: inline-flex
    align-items: center
    gap: 8px
    flex-shrink: 0

.rss-link
    display: inline-flex
    align-items: center
    gap: 5px
    padding: 3px 10px
    border: none
    border-radius: 999px
    font-family: inherit
    font-size: 0.82em
    line-height: 1.4
    color: colors.$color-text-light
    background: colors.$color-card
    text-decoration: none
    cursor: pointer
    box-shadow: 0 2px 4px -1px rgb(0 0 0 / 8%)
    transition: color 0.2s ease, transform 0.2s ease

    &:hover
        color: colors.$color-text-special
        transform: translateY(-1px)

    &.copied
        color: colors.$color-text-special

    &.mod-queue-btn.has-pending
        color: #92400e
        background: #fef3c7

.mod-pending-count
    display: inline-flex
    align-items: center
    justify-content: center
    min-width: 16px
    height: 16px
    padding: 0 5px
    border-radius: 999px
    background: #d97706
    color: #fff
    font-size: 0.85em
    font-weight: 600
    line-height: 1
</style>
