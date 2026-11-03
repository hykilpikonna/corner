export default defineNuxtPlugin(() => {
  if (typeof window === 'undefined') return

  const origLog = console.log.bind(console)
  const origInfo = console.info.bind(console)
  const origWarn = console.warn.bind(console)
  const origError = console.error.bind(console)

  function shouldSuppress(args: unknown[]): boolean {
    for (const arg of args) {
      if (typeof arg === 'string') {
        if (
          arg.includes('Calculating dimensions for') ||
          arg.includes('Spoilers initialized') ||
          arg.includes('Spoiler clicked') ||
          arg.includes('[messaging]') ||
          arg.includes('sendMessage') ||
          arg.includes('[ElInfiniteScroll]') ||
          arg.includes('v-infinite-scroll')
        ) {
          return true
        }
      } else if (arg instanceof Error) {
        const msg = `${arg.name} ${arg.message} ${arg.stack || ''}`
        if (
          msg.includes('ElInfiniteScroll') ||
          msg.includes('v-infinite-scroll') ||
          msg.includes('Calculating dimensions for') ||
          msg.includes('Spoilers initialized')
        ) {
          return true
        }
      } else if (arg && typeof arg === 'object') {
        try {
          const str = (arg as any).message || (arg as any).name || ''
          if (
            typeof str === 'string' &&
            (str.includes('ElInfiniteScroll') || str.includes('v-infinite-scroll'))
          ) {
            return true
          }
          const type = (arg as any).type
          if (type === 'getExtStorage' || type === 'matchSocialPage' || type === 'matchSitePage') {
            return true
          }
        } catch {
          // Ignore property access errors
        }
      }
    }
    return false
  }

  console.log = (...args: unknown[]) => {
    if (shouldSuppress(args)) return
    origLog(...args)
  }

  console.info = (...args: unknown[]) => {
    if (shouldSuppress(args)) return
    origInfo(...args)
  }

  console.warn = (...args: unknown[]) => {
    if (shouldSuppress(args)) return
    origWarn(...args)
  }

  console.error = (...args: unknown[]) => {
    if (shouldSuppress(args)) return
    origError(...args)
  }

  const buildTime = typeof __BUILD_TIME__ !== 'undefined' ? __BUILD_TIME__ : new Date().toISOString()
  const gitCommit = typeof __GIT_COMMIT__ !== 'undefined' ? __GIT_COMMIT__ : 'dev'
  origLog(
    `%c[Corner]%c Built at ${buildTime} (${gitCommit})`,
    'color: #42b883; font-weight: bold;',
    'color: inherit;',
  )
})
