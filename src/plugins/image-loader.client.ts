export default defineNuxtPlugin(() => {
  if (typeof window === 'undefined' || typeof MutationObserver === 'undefined') return

  const ensureEager = (img: HTMLImageElement) => {
    if (img.getAttribute('loading') === 'lazy' || img.loading === 'lazy') {
      img.loading = 'eager'
      img.setAttribute('loading', 'eager')
    }
  }

  // Update existing images
  document.querySelectorAll('img[loading="lazy"]').forEach(el => ensureEager(el as HTMLImageElement))

  // Observe dynamically added images (e.g. infinite scroll / virtualized posts)
  const observer = new MutationObserver(mutations => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (node instanceof HTMLImageElement) {
          ensureEager(node)
        } else if (node instanceof HTMLElement) {
          if (node.tagName === 'IMG') {
            ensureEager(node as HTMLImageElement)
          }
          node.querySelectorAll?.('img[loading="lazy"]').forEach(el => ensureEager(el as HTMLImageElement))
        }
      }
    }
  })

  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
  })
})
