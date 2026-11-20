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

  // Static mode must NOT render interactive controls
  await expect(page.locator('.tg-blog .quick-reaction')).toHaveCount(0)
  await expect(page.locator('.tg-blog .comment-toggle')).toHaveCount(0)

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
          display_name: 'Anonymous #a1b2',
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
        can_delete: false,
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

  const textarea = chatZone.locator('#add-comment textarea')
  await expect(textarea).toHaveAttribute('placeholder', '发消息...')
  await expect(chatZone.locator('#send-comment-btn')).toHaveCount(0)

  // Guest avatar sits left of "登录" with ~1/3 (10px of 30px) sunk behind the textarea top edge
  const guestAvatar = chatZone.locator('.guest-bar .guest-avatar')
  const signinBtn = chatZone.locator('.guest-bar .signin-btn')
  await expect(guestAvatar).toBeVisible()
  const avatarBox = await guestAvatar.boundingBox()
  const signinBox = await signinBtn.boundingBox()
  const textareaBox = await textarea.boundingBox()
  expect(avatarBox).toBeTruthy()
  expect(signinBox).toBeTruthy()
  expect(textareaBox).toBeTruthy()
  expect(avatarBox!.x + avatarBox!.width).toBeLessThanOrEqual(signinBox!.x)
  const sinkPx = avatarBox!.y + avatarBox!.height - textareaBox!.y
  expect(sinkPx).toBeGreaterThanOrEqual(7)
  expect(sinkPx).toBeLessThanOrEqual(13)

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

  // Typing reveals embedded #send-comment-btn; clicking sends with auto-solved ALTCHA PoW
  await textarea.fill('你好呀，这是一条匿名聊天消息！')
  const sendWrap = chatZone.locator('#send-comment-btn')
  await expect(sendWrap).toBeVisible()
  await sendWrap.locator('button').click()

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
  await chatZone.getByRole('button', {name: '登录'}).click()
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
    await expect(page.locator('.tg-blog .quick-reaction')).toHaveCount(0)
    await expect(page.locator('.tg-blog .comment-toggle')).toHaveCount(0)
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
