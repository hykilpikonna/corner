import {createHash} from 'node:crypto'
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

test('serves static-first prerendered /life HTML with aligned RSS copy UX, feed discovery, and zh-CN UI', async ({page, request}) => {
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

  // Stub clipboard.writeText, document.execCommand, & window.open so copy + fallback can be asserted deterministically
  await page.addInitScript(() => {
    ;(window as any).__copiedTexts = []
    ;(window as any).__execCopiedTexts = []
    ;(window as any).__openedUrls = []
    ;(window as any).__failClipboard = false
    ;(window as any).__failExecCommand = false
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: async (text: string) => {
          if ((window as any).__failClipboard) {
            throw new Error('Clipboard denied')
          }
          ;(window as any).__copiedTexts.push(text)
        },
      },
    })
    const origExecCommand = document.execCommand?.bind(document)
    document.execCommand = ((commandId: string, showUI?: boolean, value?: string) => {
      if (commandId === 'copy') {
        if ((window as any).__failExecCommand) {
          return false
        }
        const active = document.activeElement as HTMLTextAreaElement | HTMLInputElement | null
        const copied = active && typeof active.value === 'string' ? active.value : ''
        ;(window as any).__execCopiedTexts.push(copied)
        return true
      }
      return origExecCommand ? origExecCommand(commandId, showUI, value) : false
    }) as typeof document.execCommand
    window.open = ((url?: string | URL) => {
      ;(window as any).__openedUrls.push(String(url ?? ''))
      return null
    }) as typeof window.open
  })

  // 1. Inspect raw prerendered HTML before any client-side JavaScript executes
  const response = await request.get('/life')
  expect(response.status()).toBe(200)
  const rawHtml = await response.text()

  expect(rawHtml).toContain('class="tg-blog')
  const prerenderedPostIds = rawHtml.match(/id="message-\d+"/g) ?? []
  expect(prerenderedPostIds.length).toBeGreaterThan(0)
  expect(prerenderedPostIds.length).toBeLessThanOrEqual(20)

  const configuredApiUrl = (process.env.NUXT_PUBLIC_LIFE_API_URL || '').trim()

  expect(rawHtml).toMatch(/<link[^>]+rel="alternate"[^>]+type="application\/rss\+xml"/)
  expect(rawHtml).toMatch(/<link[^>]+rel="alternate"[^>]+type="application\/atom\+xml"/)
  if (configuredApiUrl) {
    expect(rawHtml).toMatch(/<link[^>]+rel="alternate"[^>]+type="application\/feed\+json"/)
  } else {
    // Default static mode must NOT emit a 404 JSON Feed alternate link when unconfigured
    expect(rawHtml).not.toMatch(/type="application\/feed\+json"/)
  }
  expect(rawHtml).toContain('搜索动态、标签或日期...')

  // 2. Hydrate in browser in default configured mode
  await page.goto('/life')
  await expect(page.locator('.tg-blog')).toBeVisible()
  await expect(page.locator('.tg-blog .post').first()).toBeVisible()

  // Feed alternate links present in document head
  await expect(page.locator('head link[rel="alternate"][type="application/rss+xml"]')).toHaveCount(1)
  await expect(page.locator('head link[rel="alternate"][type="application/atom+xml"]')).toHaveCount(1)
  await expect(page.locator('head link[rel="alternate"][type="application/feed+json"]')).toHaveCount(configuredApiUrl ? 1 : 0)

  // Verify vertical center alignment of "生活" heading and RSS pill (< 2px), and no separate open-in-new-tab button
  const heading = page.locator('.title-row h2')
  const rssLink = page.locator('.title-row .rss-link')
  await expect(page.locator('.title-row .rss-open-link')).toHaveCount(0)
  await expect(heading).toBeVisible()
  await expect(rssLink).toBeVisible()
  await expect(rssLink).toHaveAttribute('href', /rss\.xml$/)

  const h2Box = await heading.boundingBox()
  const rssBox = await rssLink.boundingBox()
  expect(h2Box).not.toBeNull()
  expect(rssBox).not.toBeNull()
  const h2CenterY = h2Box!.y + h2Box!.height / 2
  const rssCenterY = rssBox!.y + rssBox!.height / 2
  expect(Math.abs(h2CenterY - rssCenterY)).toBeLessThan(2)

  // Clicking RSS copies absolute URL and displays confirmation state
  await rssLink.click()
  await expect(rssLink).toContainText('已复制 RSS 链接')
  const copiedTexts = await page.evaluate(() => (window as any).__copiedTexts as string[])
  expect(copiedTexts).toHaveLength(1)
  expect(copiedTexts[0]).toMatch(/^https?:\/\/.*rss\.xml$/)
  expect(await page.evaluate(() => (window as any).__openedUrls as string[])).toHaveLength(0)

  // On plain HTTP (navigator.clipboard undefined), clicking RSS falls back to execCommand('copy') without opening a tab
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: undefined,
    })
  })
  await rssLink.click()
  await expect(rssLink).toContainText('已复制 RSS 链接')
  const execCopiedTexts = await page.evaluate(() => (window as any).__execCopiedTexts as string[])
  expect(execCopiedTexts).toHaveLength(1)
  expect(execCopiedTexts[0]).toMatch(/^https?:\/\/.*rss\.xml$/)
  expect(await page.evaluate(() => (window as any).__openedUrls as string[])).toHaveLength(0)

  // Only when both navigator.clipboard and execCommand fail does clicking RSS fall back to opening in a new tab
  await page.evaluate(() => {
    ;(window as any).__failExecCommand = true
  })
  await rssLink.click()
  const openedUrls = await page.evaluate(() => (window as any).__openedUrls as string[])
  expect(openedUrls).toHaveLength(1)
  expect(openedUrls[0]).toMatch(/^https?:\/\/.*rss\.xml$/)

  await expect(page.locator('.tg-blog .search input')).toHaveAttribute('placeholder', '搜索动态、标签或日期...')

  if (!configuredApiUrl) {
    // Static mode must NOT render interactive controls
    await expect(page.locator('.tg-blog .quick-reaction')).toHaveCount(0)
    await expect(page.locator('.tg-blog .comment-toggle')).toHaveCount(0)
  }

  // Flush top image: non-forwarded image-first card should have 0px top gap between card top and .images top
  const imageFirstCard = page.locator('.tg-blog .post.tgb-card:has(> .images:not(.has-head))').first()
  await expect(imageFirstCard).toBeVisible()
  const cardBox = await imageFirstCard.boundingBox()
  const imagesBox = await imageFirstCard.locator('> .images').boundingBox()
  expect(cardBox).not.toBeNull()
  expect(imagesBox).not.toBeNull()
  expect(Math.abs(imagesBox!.y - cardBox!.y)).toBeLessThan(1.5)

  expect(pageErrors).toEqual([])
  expect(consoleErrors).toEqual([])
  expect(hydrationWarnings).toEqual([])
})

test('supports /life Telegram Web A interactive mode (ReactionSelector, external chat-zone, auto-ALTCHA send, auth modal)', async ({page, request}) => {
  const pageErrors: string[] = []
  const consoleErrors: string[] = []

  page.on('pageerror', error => pageErrors.push(error.message))
  page.on('console', msg => {
    if (msg.type() === 'error') consoleErrors.push(msg.text())
  })

  const {fullArchive} = await buildDeterministicArchive(request)
  await mockPostsArchive(page, fullArchive)

  const altchaSalt = 'e2e-life-salt:'
  const altchaSecretNumber = 7
  const altchaChallengeHex = createHash('sha256')
    .update(`${altchaSalt}${altchaSecretNumber}`)
    .digest('hex')

  let targetPostId: number | null = null
  let heartReacted = false
  let receivedAltchaPayload: string | null = null
  let commentsStore: Array<Record<string, unknown>> = []
  let commentReactionsStore: Array<{
    reaction_key: string
    emoji: string | null
    custom_emoji_id: string | null
    tg_count: number
    web_count: number
    count: number
    reacted: boolean
  }> = []
  const commentReactionPayloads: Array<{emoji?: string; custom_emoji_id?: string}> = []

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
        targetPostId = ids[0]!
        commentReactionsStore = [
          {
            reaction_key: 'e:❤️',
            emoji: '❤️',
            custom_emoji_id: null,
            tg_count: 1,
            web_count: 0,
            count: 1,
            reacted: false,
          },
        ]
        commentsStore = [
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
            body_text: '欢迎来聊天！',
            body_html: '<p>欢迎来聊天！</p>',
            media: null,
            created_at: '2026-11-19T09:00:00Z',
            edited_at: null,
            deleted: false,
            is_mine: false,
            can_delete: false,
            reactions: commentReactionsStore,
          },
        ]
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
          comment_count: commentsStore.length,
        }
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          allow_anon_comments: true,
          captcha_provider: 'altcha',
          me: null,
          allowed_reactions: [
            {reaction_key: 'e:🔥', emoji: '🔥', custom_emoji_id: null},
            {reaction_key: 'e:❤️', emoji: '❤️', custom_emoji_id: null},
            {reaction_key: 'e:👍', emoji: '👍', custom_emoji_id: null},
            {reaction_key: 'e:🥰', emoji: '🥰', custom_emoji_id: null},
            {reaction_key: 'e:👏', emoji: '👏', custom_emoji_id: null},
            {reaction_key: 'e:😂', emoji: '😂', custom_emoji_id: null},
            {reaction_key: 'e:😮', emoji: '😮', custom_emoji_id: null},
            {reaction_key: 'e:😢', emoji: '😢', custom_emoji_id: null},
            {reaction_key: 'e:🎉', emoji: '🎉', custom_emoji_id: null},
            {reaction_key: 'e:✨', emoji: '✨', custom_emoji_id: null},
          ],
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

    if (path.includes('/api/life/comments/') && path.endsWith('/reactions/toggle') && req.method() === 'POST') {
      const payload = JSON.parse(req.postData() || '{}') as {emoji?: string; custom_emoji_id?: string}
      commentReactionPayloads.push(payload)
      const key = payload.custom_emoji_id ? `c:${payload.custom_emoji_id}` : `e:${payload.emoji ?? ''}`
      const idx = commentReactionsStore.findIndex(r => r.reaction_key === key)
      if (idx >= 0) {
        const cur = commentReactionsStore[idx]!
        const nextReacted = !cur.reacted
        const nextWeb = Math.max(0, cur.web_count + (nextReacted ? 1 : -1))
        const nextCount = cur.tg_count + nextWeb
        if (nextCount <= 0) {
          commentReactionsStore.splice(idx, 1)
        } else {
          commentReactionsStore[idx] = {
            ...cur,
            reacted: nextReacted,
            web_count: nextWeb,
            count: nextCount,
          }
        }
      } else {
        commentReactionsStore.push({
          reaction_key: key,
          emoji: payload.emoji ?? null,
          custom_emoji_id: payload.custom_emoji_id ?? null,
          tg_count: 0,
          web_count: 1,
          count: 1,
          reacted: true,
        })
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          post_id: targetPostId,
          comment_id: 101,
          reactions: commentReactionsStore,
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
          comments: commentsStore,
        }),
      })
      return
    }

    if (path.includes('/api/life/posts/') && path.endsWith('/comments') && req.method() === 'POST') {
      const payload = JSON.parse(req.postData() || '{}') as {
        body?: string
        parent_id?: number | null
        as_anonymous?: boolean
        altcha_payload?: string | null
      }
      receivedAltchaPayload = payload.altcha_payload ?? null
      const created = {
        id: 102,
        post_id: targetPostId,
        parent_id: payload.parent_id ?? null,
        source: 'web',
        author: {
          display_name: 'Anon #a1b2',
          avatar_url: null,
          role: 'anonymous',
          provider: 'anonymous',
          username: null,
          verified: false,
        },
        body_text: payload.body ?? '',
        body_html: `<p>${payload.body ?? ''}</p>`,
        media: null,
        created_at: '2026-11-19T09:05:00Z',
        edited_at: null,
        deleted: false,
        is_mine: true,
        can_delete: true,
      }
      commentsStore.push(created)
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify(created),
      })
      return
    }

    if (path.endsWith('/api/life/captcha/challenge')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          algorithm: 'SHA-256',
          challenge: altchaChallengeHex,
          maxnumber: 10,
          salt: altchaSalt,
          signature: 'e2e-sig',
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
          providers: ['github', 'oidc', 'email'],
          oidc_provider_name: 'HyID',
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
  const firstEntry = page.locator('.tg-blog .post-entry').first()
  const firstCard = firstEntry.locator('.post.tgb-card')
  await expect(firstCard).toBeVisible()

  // Wait for interactive hydration on the first post (Apple emoji rendered as <img class="tgb-apple-emoji">)
  const heartBtn = firstCard.locator('.interactive-reactions .reaction-btn:has(img.tgb-apple-emoji[alt="❤️"])')
  await expect(heartBtn).toBeVisible({timeout: 10_000})
  await expect(heartBtn).toContainText('3')

  // Desktop hover quick-reaction heart bubble must NOT exist; right-click opens Telegram Web A 2-stage ReactionSelector
  const cardWrap = firstEntry.locator('.post-card-wrap')
  await cardWrap.hover()
  await expect(cardWrap.locator('.quick-reaction')).toHaveCount(0)
  await firstCard.locator('.info .date').click({button: 'right'})

  const selector = page.locator('.ReactionSelector')
  await expect(selector).toBeVisible({timeout: 10_000})
  await expect(selector.locator('.ReactionSelector__reactions')).toBeVisible()
  await expect(selector.locator('.ReactionSelector__reaction img.tgb-apple-emoji').first()).toBeVisible()

  // Expand to multi-row ReactionPickerGrid and pick ❤️
  const showMoreBtn = selector.locator('.ReactionSelector__show-more')
  await expect(showMoreBtn).toBeVisible()
  await showMoreBtn.click()
  await expect(selector.locator('.ReactionPickerGrid')).toBeVisible()
  await selector.locator('.ReactionPickerGrid__cell:has(img.tgb-apple-emoji[alt="❤️"])').first().click()

  // Selector closes and reaction count updates 3 -> 4 with .chosen class
  await expect(selector).toBeHidden()
  await expect(heartBtn).toHaveClass(/chosen/)
  await expect(heartBtn).toContainText('4')

  // Escape also closes ReactionSelector when opened via right-click
  await firstCard.locator('.info .date').click({button: 'right'})
  await expect(selector).toBeVisible({timeout: 10_000})
  await page.keyboard.press('Escape')
  await expect(selector).toBeHidden()

  // Open external chat-zone beneath .post.tgb-card via the borderless round/pill replies button with translateY(-5px)
  const commentToggle = cardWrap.locator('.comment-toggle')
  await expect(commentToggle).toBeVisible()
  await expect(commentToggle).toHaveCSS('border-top-width', '0px')
  await expect(commentToggle).toHaveCSS('transform', 'matrix(1, 0, 0, 1, 0, -5)')
  await expect(commentToggle.locator('.comment-badge')).toHaveCount(0)
  await expect(commentToggle.locator('.comment-count')).toHaveText('1')
  const toggleBox = await commentToggle.boundingBox()
  const cardWrapBox = await cardWrap.boundingBox()
  expect(toggleBox).toBeTruthy()
  expect(cardWrapBox).toBeTruthy()
  expect(toggleBox!.width).toBeGreaterThanOrEqual(36)
  expect(toggleBox!.height).toBeGreaterThanOrEqual(36)
  expect(toggleBox!.x + toggleBox!.width).toBeGreaterThan(cardWrapBox!.x + cardWrapBox!.width)
  expect(toggleBox!.y + toggleBox!.height).toBeGreaterThan(cardWrapBox!.y + cardWrapBox!.height)
  await commentToggle.click()

  // Verify .chat-zone is rendered OUTSIDE .post.tgb-card (inside .chat-accordion-shell sibling of .post-card-wrap) with symmetric 1px borders
  const chatShell = firstEntry.locator('> .chat-accordion-shell')
  const chatZone = chatShell.locator('.chat-zone')
  await expect(chatZone).toBeVisible({timeout: 10_000})
  await expect(firstCard.locator('.chat-zone')).toHaveCount(0)

  const borderWidths = await chatZone.evaluate(el => ({
    before: window.getComputedStyle(el, '::before').width,
    after: window.getComputedStyle(el, '::after').width,
  }))
  expect(borderWidths).toEqual({before: '1px', after: '1px'})

  // Verify incoming owner bubble renders chronologically with zh-CN badge & bottom-right .message-timestamp (date by default, HH:mm on hover)
  const incomingBubble = chatZone.locator('#comment-101')
  await expect(incomingBubble.getByText('欢迎来聊天！')).toBeVisible()
  await expect(incomingBubble.getByText('站长')).toBeVisible()
  await expect(chatZone.locator('.MessageMeta')).toHaveCount(0)
  await expect(chatZone.locator('.bubble-hover-time')).toHaveCount(0)

  const incomingTs = incomingBubble.locator('.message-content .message-timestamp')
  await expect(incomingTs).toBeVisible()
  const tsDate = incomingTs.locator('.ts-date')
  const tsTime = incomingTs.locator('.ts-time')
  await expect(tsDate).toHaveText(/^\d{2}-\d{2}$|^\d{4}-\d{2}-\d{2}$/)
  await expect(tsTime).toHaveText(/^\d{2}:\d{2}$/)
  await expect(tsDate).toHaveCSS('opacity', '1')
  await expect(tsTime).toHaveCSS('opacity', '0')
  // Hovering the message body must keep showing the date; only hovering the date itself reveals the time
  await incomingBubble.locator('.text-content .comment-body').hover()
  await expect(tsDate).toHaveCSS('opacity', '1')
  await expect(tsTime).toHaveCSS('opacity', '0')
  await incomingTs.hover()
  await expect(tsDate).toHaveCSS('opacity', '0')
  await expect(tsTime).toHaveCSS('opacity', '1')

  // Verify card stays flush against the external chat zone with constant margin-bottom (no gap jump during accordion toggle)
  await expect(firstCard).toHaveCSS('margin-bottom', '0px')
  const cardBox = await firstCard.boundingBox()
  const chatBox = await chatZone.boundingBox()
  expect(cardBox).toBeTruthy()
  expect(chatBox).toBeTruthy()
  expect(Math.abs(chatBox!.y - (cardBox!.y + cardBox!.height))).toBeLessThanOrEqual(1)

  const textbox = chatZone.locator('#add-comment [role="textbox"]')
  await expect(textbox).toHaveAttribute('aria-label', '发消息...')
  await expect(chatZone.locator('#send-comment-btn .unstyled-button')).toHaveCount(0)

  // Composer avatar sits left of "登录" and sinks behind the composer input top edge
  const guestAvatar = chatZone.locator('.composer-bar .composer-avatar')
  const signinBtn = chatZone.locator('.composer-bar .signin-btn')
  await expect(guestAvatar).toBeVisible()
  const avatarBox = await guestAvatar.boundingBox()
  const signinBox = await signinBtn.boundingBox()
  const inputBox = await textbox.boundingBox()
  expect(avatarBox).toBeTruthy()
  expect(signinBox).toBeTruthy()
  expect(inputBox).toBeTruthy()
  expect(avatarBox!.x + avatarBox!.width).toBeLessThanOrEqual(signinBox!.x)
  const sinkPx = avatarBox!.y + avatarBox!.height - inputBox!.y
  expect(sinkPx).toBeGreaterThanOrEqual(7)
  expect(sinkPx).toBeLessThanOrEqual(20)

  // Click existing comment reaction pill on #comment-101 -> toggles 1 -> 2 with .chosen
  const commentHeartPill = incomingBubble.locator('.comment-reactions .comment-reaction-btn:has(img.tgb-apple-emoji[alt="❤️"])')
  await expect(commentHeartPill).toBeVisible()
  await expect(commentHeartPill).toContainText('1')
  await commentHeartPill.click()
  await expect(commentHeartPill).toHaveClass(/chosen/)
  await expect(commentHeartPill).toContainText('2')

  // PC has no hover reaction button on comments; right-clicking the bubble opens ReactionSelector in backend order
  await expect(incomingBubble.locator('.bubble-act-btn.react-btn')).toHaveCount(0)
  await incomingBubble.locator('.message-content').click({button: 'right'})
  await expect(selector).toBeVisible({timeout: 10_000})
  // First reaction reflects backend allowed_reactions order (🔥 first)
  await expect(selector.locator('.ReactionSelector__reaction').first().locator('img.tgb-apple-emoji')).toHaveAttribute('alt', '🔥')
  await selector.locator('.ReactionSelector__reaction:has(img.tgb-apple-emoji[alt="🔥"])').first().click()
  await expect(selector).toBeHidden()
  const commentFirePill = incomingBubble.locator('.comment-reactions .comment-reaction-btn:has(img.tgb-apple-emoji[alt="🔥"])')
  await expect(commentFirePill).toBeVisible()
  await expect(commentFirePill).toHaveClass(/chosen/)
  await expect(commentFirePill).toContainText('1')
  expect(commentReactionPayloads).toEqual([{emoji: '❤️'}, {emoji: '🔥'}])

  // Start a reply to #comment-101 so #102 renders an inline reply quote
  await incomingBubble.hover()
  await incomingBubble.locator('.bubble-act-btn[title="讨论"]').click()
  await expect(chatZone.locator('.reply-banner')).toContainText('回复 @Aza')

  // Typing reveals embedded send button; clicking sends with auto-solved ALTCHA PoW
  await textbox.click()
  await textbox.fill('你好呀，这是一条匿名聊天消息！')
  const sendBtn = chatZone.locator('#send-comment-btn .unstyled-button')
  await expect(sendBtn).toBeVisible()
  await sendBtn.click()

  // New comment appears as a right-aligned .Message.own bubble with bottom-right .message-timestamp (no empty .message-title) and solved ALTCHA
  const ownBubble = chatZone.locator('.chat-list .Message.own')
  await expect(ownBubble).toBeVisible({timeout: 10_000})
  await expect(ownBubble).toContainText('你好呀，这是一条匿名聊天消息！')
  await expect(ownBubble.locator('.message-title')).toHaveCount(0)
  await expect(ownBubble.locator('.message-content .text-content .message-timestamp.in-text .ts-date')).toHaveText(/^\d{2}-\d{2}$|^\d{4}-\d{2}-\d{2}$/)
  // Own chat bubbles must share the same background color as incoming bubbles
  const ownBg = await ownBubble.locator('.message-content').evaluate(el => window.getComputedStyle(el).backgroundColor)
  const incomingBg = await incomingBubble.locator('.message-content').evaluate(el => window.getComputedStyle(el).backgroundColor)
  expect(ownBg).toBe(incomingBg)
  expect(receivedAltchaPayload).toBeTruthy()
  const decodedAltcha = JSON.parse(Buffer.from(receivedAltchaPayload!, 'base64').toString('utf8')) as {
    number: number
    salt: string
  }
  expect(decodedAltcha.number).toBe(altchaSecretNumber)
  expect(decodedAltcha.salt).toBe(altchaSalt)

  // Click inline reply quote on #102 -> highlights #101 with drop-shadow filter (outlining bubble + appendix)
  const inlineQuote = ownBubble.locator('.inline-reply-quote')
  await expect(inlineQuote).toBeVisible()
  await inlineQuote.click()
  await expect(incomingBubble).toHaveClass(/is-highlighted/)
  const highlightStyles = await incomingBubble.locator('.message-content').evaluate(el => {
    const cs = window.getComputedStyle(el)
    return {filter: cs.filter, animationName: cs.animationName}
  })
  expect(highlightStyles.animationName).toContain('tgb-bubble-highlight')
  expect(highlightStyles.filter).toContain('drop-shadow')

  // Verify chronological order: incoming #101 before outgoing #102
  const messageIds = await chatZone.locator('.chat-list .Message').evaluateAll(els => els.map(el => el.id))
  expect(messageIds).toEqual(['comment-101', 'comment-102'])

  // Open Auth Modal from chat composer and verify zh-CN GitHub, generic OIDC, and email providers
  await chatZone.getByRole('button', {name: '登录', exact: true}).click()
  const dialog = page.getByRole('dialog', {name: '登录'})
  await expect(dialog).toBeVisible({timeout: 10_000})
  await expect(dialog.getByRole('link', {name: '使用 GitHub 登录'})).toBeVisible()
  const oidcLink = dialog.getByRole('link', {name: '使用 HyID 登录'})
  await expect(oidcLink).toBeVisible()
  await expect(oidcLink).toHaveAttribute('href', /\/auth\/oauth\/oidc\/login/)
  await expect(dialog.locator('input[placeholder="输入你的邮箱地址"]')).toBeVisible()
  await expect(dialog.getByRole('button', {name: '发送登录链接'})).toBeVisible()

  // Close Auth Modal and verify smooth accordion collapse hides .chat-accordion-shell
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await commentToggle.click()
  await expect(chatShell).toBeHidden({timeout: 5_000})

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

  const configuredApiUrl = (process.env.NUXT_PUBLIC_LIFE_API_URL || '').trim()

  page.on('pageerror', error => pageErrors.push(error.message))
  page.on('console', msg => {
    if (msg.type() === 'error') consoleErrors.push(msg.text())
  })
  page.on('request', req => {
    const parsed = new URL(req.url())
    if (
      parsed.hostname === 'evil.test'
      || parsed.pathname.includes('evil.test')
      || (!configuredApiUrl && (parsed.pathname.includes('/interactions') || parsed.pathname.includes('/stream')))
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
    if (!configuredApiUrl) {
      await expect(page.locator('.tg-blog .quick-reaction')).toHaveCount(0)
      await expect(page.locator('.tg-blog .comment-toggle')).toHaveCount(0)
    }
  }

  expect(evilRequests).toEqual([])
  expect(pageErrors).toEqual([])
  expect(consoleErrors).toEqual([])
})

test('hides mobile reaction/reply buttons and supports touch slide-to-reply', async ({browser, request}) => {
  const context = await browser.newContext({hasTouch: true, isMobile: true, viewport: {width: 390, height: 844}})
  const page = await context.newPage()
  try {
    const {previewPosts, fullArchive} = await buildDeterministicArchive(request)
    await mockPostsArchive(page, fullArchive)
    const firstPostId = previewPosts[previewPosts.length - 1]!.id

    await page.route('**/api/life/**', async route => {
      const path = new URL(route.request().url()).pathname
      if (path.endsWith('/api/life/interactions')) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            allow_anon_comments: true,
            captcha_provider: 'altcha',
            me: null,
            posts: {
              [String(firstPostId)]: {reactions: [], poll: null, comment_count: 1},
            },
          }),
        })
        return
      }
      if (path.endsWith(`/api/life/posts/${firstPostId}/comments`)) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            post_id: firstPostId,
            comments: [
              {
                id: 201,
                post_id: firstPostId,
                parent_id: null,
                source: 'web',
                author: {display_name: 'Aza', avatar_url: null, role: 'owner', provider: 'telegram', username: 'hykilp', verified: true},
                body_text: 'Mobile swipe test',
                body_html: '<p>Mobile swipe test</p>',
                media: null,
                created_at: '2026-11-19T12:00:00Z',
                edited_at: null,
                deleted: false,
                is_mine: false,
                can_delete: false,
                reactions: [],
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
          body: JSON.stringify({user: null, providers: ['email'], allow_anon_comments: true, telegram_bot_username: null}),
        })
        return
      }
      if (path.endsWith('/api/life/stream')) {
        await route.fulfill({status: 200, headers: {'content-type': 'text/event-stream'}, body: ': ping\n\n'})
        return
      }
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true})})
    })

    await page.goto('/life?lifeApi=/api/life')
    const firstEntry = page.locator('.tg-blog .post-entry').first()
    await firstEntry.locator('.comment-toggle').click()
    const bubble = firstEntry.locator('#comment-201')
    await expect(bubble.getByText('Mobile swipe test')).toBeVisible({timeout: 10_000})

    // Desktop hover react button removed completely; mobile reply button hidden
    await expect(bubble.locator('.bubble-act-btn.react-btn')).toHaveCount(0)
    await expect(bubble.locator('.bubble-act-btn.reply-btn')).toBeHidden()

    // Touch swipe left across the bubble triggers reply banner
    const contentEl = bubble.locator('.message-content')
    await contentEl.evaluate(el => {
      const r = el.getBoundingClientRect()
      const sx = r.left + r.width / 2
      const sy = r.top + r.height / 2
      el.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true, cancelable: true, pointerId: 7, pointerType: 'touch', clientX: sx, clientY: sy,
      }))
      window.dispatchEvent(new PointerEvent('pointermove', {
        bubbles: true, cancelable: true, pointerId: 7, pointerType: 'touch', clientX: sx - 25, clientY: sy,
      }))
      window.dispatchEvent(new PointerEvent('pointermove', {
        bubbles: true, cancelable: true, pointerId: 7, pointerType: 'touch', clientX: sx - 80, clientY: sy,
      }))
      window.dispatchEvent(new PointerEvent('pointerup', {
        bubbles: true, cancelable: true, pointerId: 7, pointerType: 'touch', clientX: sx - 80, clientY: sy,
      }))
    })

    await expect(firstEntry.locator('.reply-banner')).toContainText('回复 @Aza')
  } finally {
    await context.close()
  }
})

test('supports v3 view increment, Web A formatting, SymbolMenu emojis/stickers, AttachmentModal photos, reply thumbs, and own-comment delete', async ({page, request}) => {
  const pageErrors: string[] = []
  page.on('pageerror', err => pageErrors.push(err.message))

  const altchaSalt = 'v3-e2e-salt:'
  const altchaSecretNumber = 4
  const {createHash} = await import('node:crypto')
  const altchaChallengeHex = createHash('sha256').update(`${altchaSalt}${altchaSecretNumber}`).digest('hex')

  // Minimal 1x1 transparent PNG
  const TINY_PNG = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aW1sAAAAASUVORK5CYII=',
    'base64',
  )

  let targetPostId: number | null = null
  let nextCommentId = 302
  const viewBatchCalls: number[][] = []
  const deletedCommentIds: number[] = []
  const createdPayloads: Array<{body?: string; media?: Record<string, unknown> | null; parent_id?: number | null}> = []
  const commentsStore: Array<Record<string, unknown>> = []

  await page.route('**/api/life/**', async route => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname

    if (path.endsWith('/api/life/interactions')) {
      const ids = (url.searchParams.get('ids') || '').split(',').map(Number).filter(Number.isFinite)
      if (targetPostId === null && ids.length > 0) {
        targetPostId = ids[0]!
        commentsStore.push(
          {
            id: 300,
            post_id: targetPostId,
            parent_id: null,
            source: 'web',
            author: {display_name: 'Anonymous #64b6', avatar_url: null, role: 'anonymous', provider: null, username: null, verified: false},
            body_text: 'Anon message',
            body_html: '<p>Anon message</p>',
            media: null,
            created_at: '2026-11-20T00:55:00Z',
            edited_at: null,
            deleted: false,
            is_mine: false,
            can_delete: false,
            reactions: [],
          },
          {
            id: 301,
            post_id: targetPostId,
            parent_id: null,
            source: 'telegram',
            author: {display_name: 'TG Friend', avatar_url: null, role: 'telegram', provider: 'telegram', username: 'tgfriend', verified: true},
            body_text: 'Check this photo',
            body_html: '<p>Check this photo</p>',
            media: {media_type: 'photo', url: '/api/life/media/comments/tg_301.png', thumb: '/api/life/media/comments/tg_301.png', width: 100, height: 100},
            created_at: '2026-11-20T01:00:00Z',
            edited_at: null,
            deleted: false,
            is_mine: false,
            can_delete: false,
            reactions: [],
          },
        )
      }
      const posts: Record<string, unknown> = {}
      if (targetPostId !== null && ids.includes(targetPostId)) {
        posts[String(targetPostId)] = {reactions: [], poll: null, comment_count: commentsStore.length, views: '50'}
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({allow_anon_comments: true, captcha_provider: 'altcha', me: null, allowed_reactions: [], posts}),
      })
      return
    }

    if (path.endsWith('/api/life/posts/view') && req.method() === 'POST') {
      const body = JSON.parse(req.postData() || '{}') as {post_ids?: number[]}
      const pids = body.post_ids ?? []
      viewBatchCalls.push(pids)
      const views: Record<string, string> = {}
      for (const id of pids) {
        views[String(id)] = id === targetPostId ? '51' : '12'
      }
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({views})})
      return
    }

    if (path.endsWith('/api/life/emojis/custom')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({emojis: [{id: '9876543210', url: '/api/life/exports/emoji/9876543210.webp', format: 'webp'}]}),
      })
      return
    }

    if (path.endsWith('/api/life/exports/emoji/9876543210.webp') || path.endsWith('/api/life/media/comments/tg_301.png') || path.endsWith('/api/life/media/comments/web_uploaded.png') || path.endsWith('/api/life/media/stickers/starter/wave.webp')) {
      await route.fulfill({status: 200, contentType: 'image/png', body: TINY_PNG})
      return
    }

    if (path.endsWith('/api/life/stickers')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          packs: [{
            id: 'starter',
            name: 'starter',
            title: 'Starter',
            stickers: [{
              id: 'starter/wave.webp',
              file: 'wave.webp',
              url: '/api/life/media/stickers/starter/wave.webp',
              thumb: null,
              emoji: '👋',
              format: 'webp',
              width: 512,
              height: 512,
              is_animated: false,
              is_video: false,
            }],
          }],
        }),
      })
      return
    }

    if (path.endsWith('/api/life/comments/media') && req.method() === 'POST') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          media_type: 'photo',
          url: '/api/life/media/comments/web_uploaded.png',
          thumb: '/api/life/media/comments/web_uploaded.png',
          width: 1,
          height: 1,
          size: TINY_PNG.length,
          mime_type: 'image/png',
        }),
      })
      return
    }

    if (path.endsWith('/comments') && req.method() === 'GET') {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({post_id: targetPostId, comments: commentsStore})})
      return
    }

    if (path.endsWith('/comments') && req.method() === 'POST') {
      const payload = JSON.parse(req.postData() || '{}') as {body?: string; media?: Record<string, unknown> | null; parent_id?: number | null}
      createdPayloads.push(payload)
      const cid = nextCommentId++
      const bodyStr = payload.body ?? ''
      const bodyHtml = bodyStr
        ? `<p>${bodyStr.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/<tg-emoji emoji-id="(\d+)">([^<]*)<\/tg-emoji>/g, '<i class="custom-emoji" data-custom-emoji-id="$1">$2</i>')}</p>`
        : ''
      const created = {
        id: cid,
        post_id: targetPostId,
        parent_id: payload.parent_id ?? null,
        source: 'web',
        author: {display_name: 'Anon #v301', avatar_url: null, role: 'anonymous', provider: null, username: null, verified: false},
        body_text: bodyStr,
        body_html: bodyHtml,
        media: payload.media ?? null,
        created_at: '2026-11-20T02:00:00Z',
        edited_at: null,
        deleted: false,
        is_mine: true,
        can_delete: true,
        reactions: [],
      }
      commentsStore.push(created)
      await route.fulfill({status: 201, contentType: 'application/json', body: JSON.stringify(created)})
      return
    }

    if (path.includes('/api/life/comments/') && req.method() === 'DELETE') {
      const cid = Number(path.split('/').pop())
      deletedCommentIds.push(cid)
      const idx = commentsStore.findIndex(c => c.id === cid)
      if (idx >= 0) commentsStore.splice(idx, 1)
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, post_id: targetPostId, comment_id: cid})})
      return
    }

    if (path.endsWith('/api/life/captcha/challenge')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({algorithm: 'SHA-256', challenge: altchaChallengeHex, maxnumber: 10, salt: altchaSalt, signature: 'sig'}),
      })
      return
    }

    if (path.endsWith('/api/life/auth/me')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({user: null, providers: ['email'], allow_anon_comments: true, telegram_bot_username: null})})
      return
    }

    if (path.endsWith('/api/life/stream')) {
      await route.fulfill({status: 200, headers: {'content-type': 'text/event-stream'}, body: ': ping\n\n'})
      return
    }

    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true})})
  })

  await page.goto('/life?lifeApi=/api/life')
  const firstEntry = page.locator('.tg-blog .post-entry').first()
  const firstCard = firstEntry.locator('.post.tgb-card')

  // 1. View count increments on visible post via IntersectionObserver -> POST /posts/view
  await expect.poll(() => viewBatchCalls.flat().length, {timeout: 5_000}).toBeGreaterThan(0)
  await expect(firstCard.locator('.info .views')).toContainText('51')

  // Open comments and check anon avatar/ring (#300) + photo comment (#301)
  await firstEntry.locator('.comment-toggle').click()
  const chatZone = firstEntry.locator('.chat-zone')
  const anonComment = chatZone.locator('#comment-300')
  await expect(anonComment).toBeVisible({timeout: 10_000})
  await expect(anonComment.locator('.sender-title')).toHaveText('Anon #64b6')
  const anonAvatarImg = anonComment.locator('img.bubble-avatar.anon.anon-ringed')
  await expect(anonAvatarImg).toHaveAttribute('src', /^\/anons\/anon-[1-9]\.webp$/)
  const anonRingColor = await anonAvatarImg.evaluate(el => (el as HTMLElement).style.getPropertyValue('--tgb-anon-ring'))
  expect(anonRingColor).toMatch(/^#[0-9a-f]{6}$/i)

  const photoImg = chatZone.locator('#comment-301 .comment-media-photo img')
  await expect(photoImg).toBeVisible({timeout: 10_000})

  // Right-clicking photo comment opens ReactionSelector instead of native browser menu
  await photoImg.click({button: 'right'})
  await expect(page.locator('.tgb-reaction-selector, .ReactionSelector').first()).toBeVisible({timeout: 5_000})
  await page.keyboard.press('Escape')
  await expect(page.locator('.tgb-reaction-selector, .ReactionSelector')).toHaveCount(0)

  // 2. Reply to photo comment #301 using Web A formatting (right-click -> Bold) + SymbolMenu custom emoji
  await chatZone.locator('#comment-301').hover()
  await chatZone.locator('#comment-301 .bubble-act-btn[title="讨论"]').click()

  const textbox = chatZone.locator('#add-comment [role="textbox"]')
  await textbox.click()
  await textbox.type('hello world')
  // Select "hello" programmatically then right-click directly over the selection to open TextFormatter
  const selPoint = await textbox.evaluate(el => {
    const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
    const textNode = walk.nextNode() as Text | null
    if (!textNode) return null
    const r = document.createRange()
    r.setStart(textNode, 0)
    r.setEnd(textNode, 5)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(r)
    const rect = r.getBoundingClientRect()
    return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2}
  })
  expect(selPoint).toBeTruthy()
  await page.mouse.click(selPoint!.x, selPoint!.y, {button: 'right'})
  const formatter = page.locator('.TextFormatter')
  await expect(formatter).toBeVisible({timeout: 5_000})
  await formatter.getByRole('button', {name: '加粗'}).click()

  // Close formatter, move caret to end of text, and insert a custom emoji via SymbolMenu
  await page.keyboard.press('Escape')
  await textbox.focus()
  await page.keyboard.press('End')
  await chatZone.locator('.composer-smiley-btn').click()
  const symbolMenu = page.locator('.SymbolMenu')
  await expect(symbolMenu).toBeVisible({timeout: 5_000})
  await symbolMenu.locator('.custom-grid .EmojiButton').first().click()
  await page.locator('.tgb-symbol-backdrop').click({position: {x: 2, y: 2}})

  // Char count updated reactively > 0; send formatted reply
  await expect(chatZone.locator('#send-comment-btn .char-count')).not.toHaveText('0')
  await chatZone.locator('#send-comment-btn .unstyled-button').click()

  const replyBubble = chatZone.locator('#comment-302')
  await expect(replyBubble).toBeVisible({timeout: 10_000})
  await expect(chatZone.locator('.composer-bar .composer-author-name')).toHaveText('Anon #v301')
  const v3GuestAvatar = chatZone.locator('.composer-bar .composer-avatar.anon-ringed')
  await expect(v3GuestAvatar.locator('img.composer-avatar-img')).toHaveAttribute('src', /^\/anons\/anon-[1-9]\.webp$/)
  await expect.poll(async () => (await v3GuestAvatar.boundingBox())?.width, {timeout: 5_000}).toBe(40)
  await expect.poll(async () => (await v3GuestAvatar.boundingBox())?.height, {timeout: 5_000}).toBe(40)
  const guestHintBox = await chatZone.locator('.composer-bar .guest-hint').boundingBox()
  const v3SigninBox = await chatZone.locator('.composer-bar .signin-btn').boundingBox()
  expect(guestHintBox!.x + guestHintBox!.width).toBeLessThanOrEqual(v3SigninBox!.x)
  // Clicking the guest avatar opens the auth modal
  await v3GuestAvatar.click()
  await expect(page.locator('.tgb-auth-backdrop')).toBeVisible({timeout: 5_000})
  await page.locator('.tgb-auth-backdrop .close-btn').click()
  await expect(page.locator('.tgb-auth-backdrop')).toHaveCount(0)
  await expect(replyBubble.locator('.inline-reply-quote .quote-thumb')).toBeVisible()
  await expect(replyBubble.locator('.comment-body b')).toContainText('hello')
  expect(createdPayloads[0]?.body).toContain('**hello**')
  expect(createdPayloads[0]?.body).toContain('<tg-emoji emoji-id="9876543210">')

  // 3. Send 1-click sticker from SymbolMenu Stickers tab
  await chatZone.locator('.composer-smiley-btn').click()
  await expect(symbolMenu).toBeVisible()
  await symbolMenu.locator('.SymbolMenu-footer .symbol-tab-button').nth(1).click()
  await expect(symbolMenu.locator('.StickerButton')).toHaveCount(1)
  await symbolMenu.locator('.StickerButton').first().click()
  await expect(chatZone.locator('#comment-303 .comment-media-sticker')).toBeVisible({timeout: 10_000})

  // 4. Upload photo via AttachmentModal and send with caption
  await chatZone.locator('input[type="file"]').setInputFiles({
    name: 'photo.png',
    mimeType: 'image/png',
    buffer: TINY_PNG,
  })
  const attachModal = page.locator('.tgb-attach-modal')
  await expect(attachModal).toBeVisible({timeout: 5_000})
  await attachModal.getByRole('button', {name: '发送图片'}).click()
  await expect(chatZone.locator('#comment-304 .comment-media-photo img')).toBeVisible({timeout: 10_000})

  // 5. Delete own anonymous comment (#comment-304) with 2-step confirm
  await chatZone.locator('#comment-304').hover()
  const delBtn = chatZone.locator('#comment-304 .bubble-act-btn.danger')
  await expect(delBtn).toBeVisible()
  await delBtn.click()
  await expect(delBtn).toContainText('确认删除')
  await delBtn.click()
  await expect(chatZone.locator('#comment-304')).toHaveCount(0, {timeout: 5_000})
  expect(deletedCommentIds).toEqual([304])
  expect(pageErrors).toEqual([])
})
