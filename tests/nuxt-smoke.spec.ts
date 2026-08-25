import {expect, test} from '@playwright/test'

test('preserves SPA navigation and interactive transitions', async ({page}) => {
  const pageErrors: string[] = []
  const consoleErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message))
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })

  await page.goto('/')
  await page.evaluate(() => ((window as any).__cornerNavigationMarker = 'alive'))

  const app = page.locator('#app')
  await expect(app).toHaveCSS('color', 'rgb(112, 81, 42)')
  await expect(app).toHaveCSS('display', 'flex')
  await expect(app).toHaveCSS('height', '720px')

  const homeBox = await page.locator('#box').boundingBox()
  expect(homeBox).not.toBeNull()
  expect(Math.abs(homeBox!.x + homeBox!.width / 2 - 640)).toBeLessThan(2)
  expect(Math.abs(homeBox!.y + homeBox!.height / 2 - 360)).toBeLessThan(2)

  const bookmark = page.locator('#nav-bookmark')
  await expect(bookmark).toHaveAttribute('style', /top:/)
  await expect(bookmark).toHaveCSS('transition-duration', '0.25s')
  const homeTop = await bookmark.evaluate(element => (element as HTMLElement).style.top)

  await page.locator('#items a[href="/about"]').click()
  await page.waitForURL('**/about')
  expect(await page.evaluate(() => (window as any).__cornerNavigationMarker)).toBe('alive')
  await expect(page.locator('#About')).toBeVisible()
  await expect(bookmark).not.toHaveCSS('top', homeTop)

  const firstCollapse = page.locator('.collapse').first()
  const collapseContent = firstCollapse.locator('.content')
  const initiallyVisible = await collapseContent.isVisible()
  await firstCollapse.locator('h3').click()
  await expect(collapseContent)[initiallyVisible ? 'toBeHidden' : 'toBeVisible']()

  await page.locator('#items a[href="/blog"]').click()
  await page.waitForURL('**/blog')
  const posts = page.locator('#BlogPostPreview')
  expect(await posts.count()).toBeGreaterThan(1)
  // The pinned post (Index) opens by default; exactly one post is expanded
  await expect(posts.locator('.content:visible')).toHaveCount(1)

  // The pinned post (Index) starts open; clicking another post switches to it
  const firstPost = posts.nth(0)
  const secondPost = posts.nth(1)
  await expect(firstPost.locator('.content')).toBeVisible()

  await secondPost.locator('#titles').click()
  await page.waitForURL(url => url.searchParams.get('post') !== 'Index' && url.searchParams.get('post') !== null)
  await expect(secondPost.locator('.content')).toBeVisible()
  await expect(firstPost.locator('.content')).toBeHidden()

  await page.goBack()
  await expect(firstPost.locator('.content')).toBeVisible()
  await expect(secondPost.locator('.content')).toBeHidden()

  expect(pageErrors).toEqual([])
  expect(consoleErrors).toEqual([])
})

test('hydrates the gallery and Telegram blog', async ({page}) => {
  const pageErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message))

  // Pick a photo id dynamically — live manifest changes as photos are added
  await page.goto('/photo')
  const containers = page.locator('.img-container')
  expect(await containers.count()).toBeGreaterThan(0)
  const photoId = await containers.first().getAttribute('id')
  expect(photoId).toMatch(/^photo-/)

  await page.goto(`/${photoId!.replace('photo-', 'photo/')}`)
  await expect(page.locator('.img-container').first()).toBeVisible()
  await expect(page.locator(`#${photoId}`)).toHaveClass(/active/)
  await expect(page.locator('.blur')).toBeVisible()

  await page.locator(`#${photoId}`).click()
  await expect(page.locator(`#${photoId}`)).not.toHaveClass(/active/)
  await expect(page.locator('.blur')).toBeHidden()

  await page.locator('#items a[href="/life"]').click()
  await page.waitForURL('**/life')
  await expect(page.locator('.tg-blog')).toBeVisible({timeout: 15_000})

  expect(pageErrors).toEqual([])
})

test('keeps the original alignment and color contract across routes', async ({page}) => {

  const centeredPages = [
    ['/about', '#About', 'justify'],
    ['/blog', '#Blog', 'left'],
    ['/others', '#Others', 'left'],
    ['/friends', '#Friends', 'left'],
    ['/kitchen-menu', '#Menu', 'left'],
  ] as const

  for (const [path, selector, textAlign] of centeredPages) {
    await page.goto(path)
    const app = page.locator('#app')
    const content = page.locator(selector)

    await expect(app).toHaveCSS('color', 'rgb(112, 81, 42)')
    await expect(app).toHaveCSS('max-width', '900px')
    await expect(content).toHaveCSS('text-align', textAlign)

    const box = await content.boundingBox()
    expect(box).not.toBeNull()
    expect(Math.abs(box!.x + box!.width / 2 - 640)).toBeLessThan(2)
  }

  for (const path of ['/photo', '/life']) {
    await page.goto(path)
    const appBox = await page.locator('#app').boundingBox()
    expect(appBox).not.toBeNull()
    expect(appBox!.width).toBe(900)
    expect(Math.abs(appBox!.x + appBox!.width / 2 - 640)).toBeLessThan(2)
    await expect(page.locator('#app')).toHaveCSS('color', 'rgb(112, 81, 42)')
  }
})
