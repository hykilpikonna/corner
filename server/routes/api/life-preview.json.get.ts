// Build-time preview of the newest 20 non-service Telegram posts for /life.
// Prerendered to a static /api/life-preview.json by `nuxt generate`, so
// /life/index.html ships with real prerendered post HTML for instant first paint
// and SEO while the client seamlessly hydrates the full posts.json archive.
// Lives under server/routes (not server/api) so the .json extension is part of
// the URL — static hosts then serve it with the correct application/json type.
const DEFAULT_LIFE_POSTS_URL = 'https://profile-api.hydev.org/exports/hykilp/posts.json'
const PREVIEW_POST_COUNT = 20

interface PreviewPost {
    type?: string
    [key: string]: unknown
}

export default defineEventHandler(async () => {
    const apiUrl = (process.env.NUXT_PUBLIC_LIFE_API_URL || '').replace(/\/+$/, '')
    const postsUrl = process.env.LIFE_PREVIEW_POSTS_URL
        || process.env.NUXT_PUBLIC_LIFE_POSTS_URL
        || (apiUrl ? `${apiUrl}/exports/posts.json` : DEFAULT_LIFE_POSTS_URL)

    if (!/^https?:\/\//i.test(postsUrl)) {
        console.warn(
            `[life-preview] Skipping build-time preview fetch because postsUrl "${postsUrl}" is relative; set LIFE_PREVIEW_POSTS_URL to an absolute HTTP(S) URL to prerender posts.`
        )
        return []
    }

    try {
        const posts = await $fetch<PreviewPost[]>(postsUrl, {
            timeout: 15_000,
            retry: 1,
        })
        if (!Array.isArray(posts)) {
            console.warn(`[life-preview] Expected an array from ${postsUrl}, falling back to []`)
            return []
        }
        // Keep chronological order (oldest -> newest) so TgBlog's .reverse()
        // renders newest-first, matching posts.json identically.
        return posts
            .filter(post => post && post.type !== 'service')
            .slice(-PREVIEW_POST_COUNT)
    } catch (err) {
        console.warn(`[life-preview] Failed to fetch ${postsUrl}, falling back to []:`, err)
        return []
    }
})
