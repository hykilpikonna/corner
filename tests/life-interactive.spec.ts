import {expect, test} from '@playwright/test'
import type {APIRequestContext, Page} from '@playwright/test'

interface TestPost {
  id: number
  date: string
  text?: string
  [key: string]: unknown
}

async function buildDeterministicArchive(request: APIRequestContext): Promise<{
  previewPosts: TestPost[]
  fullArchive: TestPost[]
  recentIndexedPostId: number
  archivedPost25: TestPost
}> {
  const previewRes = await request.get('/api/life-preview.json')
  expect(previewRes.status()).toBe(200)
  const previewPosts = (await previewRes.json()) as TestPost[]
  expect(previewPosts.length).toBe(20)

  const minPreviewId = Math.min(...previewPosts.map(p => p.id))
  const olderBaseId = Math.max(1, minPreviewId - 20)

  // Prepend 10 older posts in chronological order (oldest -> newest) so that when
  // TgBlog reverses the 30-post archive:
  //   indices 0..19  -> previewPosts (reversed)
  //   indices 20..29 -> olderPosts (reversed), with index 25 = olderPosts[4]
  const olderPosts: TestPost[] = Array.from({length: 10}, (_, i) => ({
    id: olderBaseId + i,
    date: `2025-01-${String(i + 1).padStart(2, '0')}T12:00:00Z`,
    text: i === 4
      ? `Deterministic archive post #25 ARCHIVE_POST_25_TOKEN`
      : `Deterministic archived filler post #${olderBaseId + i}`,
  }))

  // previewPosts[5] sits at reverse-chronological index 14 (within 11..19)
  const recentIndexedPostId = previewPosts[5]!.id
  const archivedPost25 = olderPosts[4]!

  return {
    previewPosts,
    fullArchive: [...olderPosts, ...previewPosts],
    recentIndexedPostId,
    archivedPost25,
  }
}

async function mockPostsArchive(page: Page, posts: TestPost[], delayMs = 0): Promise<void> {
  await page.route('**/posts.json', async route => {
    if (delayMs > 0) {
      await new Promise(resolve => setTimeout(resolve, delayMs))
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(posts),
    })
  })
}

test('serves static-first prerendered /life HTML with feed discovery and zh-CN UI', async ({page, request}) => {
  const pageErrors: string[] = []
  const consoleErrors: string[] = []
  const hydrationWarnings: string[] = []

  page.on('pageerror', error => pageErrors.push(error.message))
  page.on('console', msg => {
    const text = msg.text()
    if (msg.type() === 'error') consoleErrors.push(text)
    if (msg.type() === 'warning' && text.toLowerCase().includes('hydration')) {
      hydrationWarnings.push(text)
    }
  })

  const {fullArchive} = await buildDeterministicArchive(request)
  await mockPostsArchive(page, fullArchive)

  // 1. Inspect raw prerendered HTML before any client-side JavaScript executes
  const response = await request.get('/life')
  expect(response.status()).toBe(200)
  const rawHtml = await response.text()

  expect(rawHtml).toContain('class="tg-blog')
  const prerenderedPostIds = rawHtml.match(/id="message-\d+"/g) ?? []
  expect(prerenderedPostIds.length).toBeGreaterThan(0)
  expect(prerenderedPostIds.length).toBeLessThanOrEqual(20)

  expect(rawHtml).toMatch(/<link[^>]+rel="alternate"[^>]+type="application\/rss\+xml"/)
  expect(rawHtml).toMatch(/<link[^>]+rel="alternate"[^>]+type="application\/atom\+xml"/)
  // Default static mode must NOT emit a 404 JSON Feed alternate link when unconfigured
  expect(rawHtml).not.toMatch(/type="application\/feed\+json"/)
  expect(rawHtml).toContain('搜索动态、标签或日期...')

  // 2. Hydrate in browser in default static read-only mode (no apiUrl configured)
  await page.goto('/life')
  await expect(page.locator('.tg-blog')).toBeVisible()
  await expect(page.locator('.tg-blog .post').first()).toBeVisible()

  // Feed alternate links present in document head (RSS + Atom only)
  await expect(page.locator('head link[rel="alternate"][type="application/rss+xml"]')).toHaveCount(1)
  await expect(page.locator('head link[rel="alternate"][type="application/atom+xml"]')).toHaveCount(1)
  await expect(page.locator('head link[rel="alternate"][type="application/feed+json"]')).toHaveCount(0)

  // Subtle RSS link in header and Simplified Chinese search placeholder
  const rssLink = page.locator('.title .rss-link')
  await expect(rssLink).toBeVisible()
  await expect(rssLink).toHaveAttribute('href', /rss\.xml$/)
  await expect(page.locator('.tg-blog .search input')).toHaveAttribute('placeholder', '搜索动态、标签或日期...')

  // Static mode must NOT render interactive controls
  await expect(page.locator('.tg-blog .add-reaction-btn')).toHaveCount(0)
  await expect(page.locator('.tg-blog .comment-toggle')).toHaveCount(0)

  expect(pageErrors).toEqual([])
  expect(consoleErrors).toEqual([])
  expect(hydrationWarnings).toEqual([])
})

test('supports /life interactive mode (reactions, comments, auth modal) with zh-CN UI', async ({page, request}) => {
  const pageErrors: string[] = []
  const consoleErrors: string[] = []

  page.on('pageerror', error => pageErrors.push(error.message))
  page.on('console', msg => {
    if (msg.type() === 'error') consoleErrors.push(msg.text())
  })

  const {fullArchive} = await buildDeterministicArchive(request)
  await mockPostsArchive(page, fullArchive)

  let targetPostId: number | null = null
  let heartReacted = false

  await page.route('**/api/life/**', async route => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname

    if (path.endsWith('/api/life/interactions')) {
      const ids = (url.searchParams.get('ids') || '')
        .split(',')
        .map(Number)
        .filter(Number.isFinite)
      if (targetPostId === null && ids.length > 0) {
        targetPostId = ids[0]
      }
      const posts: Record<string, unknown> = {}
      if (targetPostId !== null && ids.includes(targetPostId)) {
        posts[String(targetPostId)] = {
          reactions: [
            {
              reaction_key: 'e:❤️',
              emoji: '❤️',
              custom_emoji_id: null,
              tg_count: 3,
              web_count: heartReacted ? 1 : 0,
              count: heartReacted ? 4 : 3,
              reacted: heartReacted,
            },
          ],
          poll: null,
          comment_count: 1,
        }
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          allow_anon_comments: true,
          captcha_provider: 'altcha',
          me: null,
          posts,
        }),
      })
      return
    }

    if (path.includes('/api/life/posts/') && path.endsWith('/reactions/toggle') && req.method() === 'POST') {
      heartReacted = !heartReacted
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          post_id: targetPostId,
          reactions: [
            {
              reaction_key: 'e:❤️',
              emoji: '❤️',
              custom_emoji_id: null,
              tg_count: 3,
              web_count: heartReacted ? 1 : 0,
              count: heartReacted ? 4 : 3,
              reacted: heartReacted,
            },
          ],
        }),
      })
      return
    }

    if (path.includes('/api/life/posts/') && path.endsWith('/comments') && req.method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          post_id: targetPostId,
          comments: [
            {
              id: 101,
              post_id: targetPostId,
              parent_id: null,
              source: 'web',
              author: {
                display_name: 'Aza',
                avatar_url: null,
                role: 'owner',
                provider: 'github',
                username: 'aza',
                verified: true,
              },
              body_text: '欢迎来留言！',
              body_html: '<p>欢迎来留言！</p>',
              media: null,
              created_at: '2026-11-19T09:00:00Z',
              edited_at: null,
              deleted: false,
              is_mine: false,
              can_delete: false,
            },
          ],
        }),
      })
      return
    }

    if (path.endsWith('/api/life/auth/me')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          user: null,
          providers: ['github', 'email'],
          allow_anon_comments: true,
          telegram_bot_username: null,
        }),
      })
      return
    }

    if (path.endsWith('/api/life/stream')) {
      await route.fulfill({
        status: 200,
        headers: {
          'content-type': 'text/event-stream',
          'cache-control': 'no-cache',
        },
        body: ': ping\n\n',
      })
      return
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ok: true}),
    })
  })

  await page.goto('/life?lifeApi=/api/life')
  await expect(page.locator('.tg-blog .post').first()).toBeVisible()

  // Wait for interactive hydration on the first post
  const firstPost = page.locator('.tg-blog .post').first()
  const addReactionBtn = firstPost.locator('.add-reaction-btn')
  await expect(addReactionBtn).toBeVisible({timeout: 10_000})
  await expect(addReactionBtn).toHaveAttribute('title', '添加回应')

  // Toggle reaction and verify count updates from 3 -> 4
  const heartBtn = firstPost.locator('.interactive-reactions .reaction-btn', {hasText: '❤️'})
  await expect(heartBtn).toContainText('3')
  await heartBtn.click()
  await expect(heartBtn).toHaveClass(/reacted/)
  await expect(heartBtn).toContainText('4')

  // Open comments thread and verify zh-CN strings + owner badge
  const commentToggle = firstPost.locator('.comment-toggle')
  await expect(commentToggle).toContainText('1')
  await commentToggle.click()

  await expect(firstPost.getByText('欢迎来留言！')).toBeVisible({timeout: 10_000})
  await expect(firstPost.getByText('站长')).toBeVisible()
  await expect(firstPost.locator('textarea')).toHaveAttribute('placeholder', '写下你的想法...')

  // Open Auth Modal from comments composer and verify zh-CN providers
  await firstPost.getByRole('button', {name: '登录'}).click()
  const dialog = page.getByRole('dialog', {name: '登录'})
  await expect(dialog).toBeVisible({timeout: 10_000})
  await expect(dialog.getByRole('link', {name: '使用 GitHub 登录'})).toBeVisible()
  await expect(dialog.locator('input[placeholder="输入你的邮箱地址"]')).toBeVisible()
  await expect(dialog.getByRole('button', {name: '发送登录链接'})).toBeVisible()

  expect(pageErrors).toEqual([])
  expect(consoleErrors).toEqual([])
})

test('upgrades 20-post preview to full archive and searches archived post #25', async ({page, request}) => {
  const pageErrors: string[] = []
  const consoleErrors: string[] = []

  page.on('pageerror', error => pageErrors.push(error.message))
  page.on('console', msg => {
    if (msg.type() === 'error') consoleErrors.push(msg.text())
  })

  const {fullArchive, archivedPost25} = await buildDeterministicArchive(request)
  let archiveRequested = false
  await page.route('**/posts.json', async route => {
    archiveRequested = true
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(fullArchive),
    })
  })

  await page.goto('/life')
  await expect(page.locator('.tg-blog .post')).toHaveCount(20)
  await expect.poll(() => archiveRequested, {timeout: 10_000}).toBe(true)

  // Search for archived post #25 (outside the initial 20-post SSR preview)
  const searchInput = page.locator('.tg-blog .search input')
  await searchInput.fill('ARCHIVE_POST_25_TOKEN')

  const targetPost = page.locator(`#message-${archivedPost25.id}`)
  await expect(targetPost).toBeVisible({timeout: 10_000})
  await expect(targetPost).toContainText('Deterministic archive post #25 ARCHIVE_POST_25_TOKEN')

  expect(pageErrors).toEqual([])
  expect(consoleErrors).toEqual([])
})

test('handles ?shared= for both preview-window and older archived posts cleanly', async ({page, request}) => {
  const pageErrors: string[] = []
  const consoleErrors: string[] = []
  const hydrationWarnings: string[] = []

  page.on('pageerror', error => pageErrors.push(error.message))
  page.on('console', msg => {
    const text = msg.text()
    if (msg.type() === 'error') consoleErrors.push(text)
    if (msg.type() === 'warning' && text.toLowerCase().includes('hydration')) {
      hydrationWarnings.push(text)
    }
  })

  const {fullArchive, recentIndexedPostId, archivedPost25} = await buildDeterministicArchive(request)
  await mockPostsArchive(page, fullArchive, 150)

  // (a) ?shared=<recent_id> (index 14 of the 20-post preview): zero hydration warnings
  await page.goto(`/life?shared=${recentIndexedPostId}`)
  const recentPost = page.locator(`#message-${recentIndexedPostId}`)
  await expect(recentPost).toBeVisible({timeout: 10_000})
  await expect(recentPost).toBeInViewport({timeout: 10_000})

  expect(hydrationWarnings).toEqual([])

  // (b) ?shared=<old_id> (index 25, outside top-20 preview): renders and scrolls into view once archive loads
  await page.goto(`/life?shared=${archivedPost25.id}`)
  const oldPost = page.locator(`#message-${archivedPost25.id}`)
  await expect(oldPost).toBeVisible({timeout: 10_000})
  await expect(oldPost).toBeInViewport({timeout: 10_000})

  expect(pageErrors).toEqual([])
  expect(consoleErrors).toEqual([])
  expect(hydrationWarnings).toEqual([])
})

test('rejects malicious ?lifeApi= query values without leaking requests', async ({page, request}) => {
  const pageErrors: string[] = []
  const consoleErrors: string[] = []
  const evilRequests: string[] = []

  page.on('pageerror', error => pageErrors.push(error.message))
  page.on('console', msg => {
    if (msg.type() === 'error') consoleErrors.push(msg.text())
  })
  page.on('request', req => {
    const parsed = new URL(req.url())
    if (
      parsed.hostname === 'evil.test'
      || parsed.pathname.includes('evil.test')
      || parsed.pathname.includes('/interactions')
      || parsed.pathname.includes('/stream')
    ) {
      evilRequests.push(req.url())
    }
  })

  const {fullArchive} = await buildDeterministicArchive(request)
  await mockPostsArchive(page, fullArchive)

  const maliciousPayloads = [
    'https://evil.test',
    '//evil.test',
    '/\\evil.test',
    '/api/../evil.test',
  ]

  for (const payload of maliciousPayloads) {
    await page.goto(`/life?lifeApi=${encodeURIComponent(payload)}`)
    await expect(page.locator('.tg-blog .post').first()).toBeVisible()
    // Wait briefly to ensure no delayed interactive hydration fires
    await page.waitForTimeout(300)
    await expect(page.locator('.tg-blog .add-reaction-btn')).toHaveCount(0)
    await expect(page.locator('.tg-blog .comment-toggle')).toHaveCount(0)
  }

  expect(evilRequests).toEqual([])
  expect(pageErrors).toEqual([])
  expect(consoleErrors).toEqual([])
})
