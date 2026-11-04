export default defineNuxtPlugin(() => {
  if (typeof window === 'undefined') return

  const hasIntersectionObserver = typeof IntersectionObserver !== 'undefined'

  // Image load / retry logic
  const loadImage = (img: HTMLImageElement) => {
    const dataSrc = img.getAttribute('data-src')
    if (dataSrc && img.getAttribute('src') !== dataSrc) {
      img.src = dataSrc
    }
  }

  // Fallback if IntersectionObserver is not available
  if (!hasIntersectionObserver) {
    document.querySelectorAll<HTMLImageElement>('img[data-src]').forEach(loadImage)
    return
  }

  // IntersectionObserver with generous rootMargin (600px ahead of viewport)
  const imageObserver = new IntersectionObserver(
    (entries, observer) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          const img = entry.target as HTMLImageElement
          loadImage(img)
          observer.unobserve(img)
        }
      }
    },
    {
      rootMargin: '600px 0px 600px 0px',
    }
  )

  const setupImage = (img: HTMLImageElement) => {
    // Strip native lazy loading attribute to avoid Firefox lazy-img stalls
    if (img.getAttribute('loading') === 'lazy' || img.loading === 'lazy') {
      img.removeAttribute('loading')
    }

    if (img.hasAttribute('data-src')) {
      const dataSrc = img.getAttribute('data-src')
      if (img.getAttribute('src') === dataSrc) return // already loaded

      // Attach retry on network failure
      if (!img.dataset.hasRetryListener) {
        img.dataset.hasRetryListener = 'true'
        img.addEventListener('error', () => {
          const retries = Number(img.dataset.retryCount || 0)
          if (retries < 3) {
            img.dataset.retryCount = String(retries + 1)
            const delay = 1000 * Math.pow(2, retries)
            setTimeout(() => {
              const currentSrc = img.getAttribute('data-src') || img.src
              if (currentSrc && !currentSrc.startsWith('data:')) {
                const sep = currentSrc.includes('?') ? '&' : '?'
                img.src = `${currentSrc.split('?_retry=')[0]}${sep}_retry=${Date.now()}`
              }
            }, delay)
          }
        })
      }

      imageObserver.observe(img)
    }
  }

  // Setup existing images in DOM
  document.querySelectorAll<HTMLImageElement>('img').forEach(setupImage)

  // Observe dynamically added images (e.g. infinite scroll / virtualized posts)
  if (typeof MutationObserver !== 'undefined') {
    const domObserver = new MutationObserver((mutations) => {
      for (const m of mutations) {
        for (const node of m.addedNodes) {
          if (node instanceof HTMLImageElement) {
            setupImage(node)
          } else if (node instanceof HTMLElement) {
            if (node.tagName === 'IMG') {
              setupImage(node as HTMLImageElement)
            }
            node.querySelectorAll<HTMLImageElement>('img').forEach(setupImage)
          }
        }
      }
    })

    domObserver.observe(document.documentElement, {
      childList: true,
      subtree: true,
    })
  }
})
