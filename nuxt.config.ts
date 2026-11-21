import { execSync } from 'node:child_process'

const buildTime = new Date().toISOString()
let gitCommit = process.env.GITHUB_SHA?.slice(0, 7)
if (!gitCommit) {
  try {
    gitCommit = execSync('git rev-parse --short HEAD').toString().trim()
  } catch {
    gitCommit = 'dev'
  }
}

export default defineNuxtConfig({
  srcDir: 'src/',
  ssr: true,

  modules: ['@unocss/nuxt'],
  css: ['@fortawesome/fontawesome-free/css/all.min.css'],

  app: {
    head: {
      htmlAttrs: {lang: 'zh-CN'},
      meta: [
        {charset: 'utf-8'},
        {name: 'viewport', content: 'width=device-width, initial-scale=1'},
        {name: 'description', content: "Aza's Corner"},
      ],
      link: [
        {rel: 'apple-touch-icon', sizes: '180x180', href: '/icons/apple-touch-icon.png'},
        {rel: 'icon', type: 'image/png', sizes: '32x32', href: '/icons/favicon-32x32.png'},
        {rel: 'icon', type: 'image/png', sizes: '16x16', href: '/icons/favicon-16x16.png'},
      ],
    },
  },

  build: {
    transpile: ['vue-i18n', 'tg-blog'],
  },

  vite: {
    envPrefix: ['VITE_', 'NUXT_PUBLIC_'],
    resolve: {
      dedupe: ['vue'],
    },
    plugins: [
      {
        name: 'tg-blog-dev-no-cache',
        apply: 'serve',
        configureServer(server) {
          server.middlewares.use((req, res, next) => {
            if (req.url?.includes('/tg-blog/')) {
              const origSetHeader = res.setHeader.bind(res)
              res.setHeader = ((name: string, value: number | string | readonly string[]) => {
                if (name.toLowerCase() === 'cache-control') {
                  return origSetHeader(name, 'no-cache')
                }
                return origSetHeader(name, value)
              }) as typeof res.setHeader
            }
            next()
          })
        },
      },
    ],
    define: {
      __VUE_PROD_DEVTOOLS__: 'false',
      __BUILD_TIME__: JSON.stringify(buildTime),
      __GIT_COMMIT__: JSON.stringify(gitCommit),
    },
  },

  nitro: {
    devProxy: process.env.NUXT_DEV_LIFE_API_TARGET
      ? {
          '/api/life': {
            target: `${process.env.NUXT_DEV_LIFE_API_TARGET.replace(/\/+$/, '')}/api/life`,
            changeOrigin: true,
          },
        }
      : undefined,
    replace: {
      __VUE_PROD_DEVTOOLS__: 'false',
      __BUILD_TIME__: JSON.stringify(buildTime),
      __GIT_COMMIT__: JSON.stringify(gitCommit),
    },
    prerender: {
      crawlLinks: true,
      ignore: ['/api/life', '/api/life/**'],
      routes: [
        '/',
        '/api/photos.json',
        '/api/life-preview.json',
        '/about',
        '/life',
        '/blog',
        '/photo',
        '/others',
        '/kitchen-menu',
        '/friends',
      ],
    },
    hooks: {
      'prerender:generate'(route) {
        if (route.contents && route.fileName?.endsWith('.html')) {
          route.contents = route.contents.replace(
            /<link rel="preload" as="fetch"[^>]*_payload\.json[^>]*>\s*/g,
            '',
          )
        }
      },
    },
  },

  hooks: {
    // Discover /photo/<id> routes from the live manifest so every photo gets
    // prerendered. Failure is fatal: silently shipping a gallery whose new
    // photos all 404 is worse than a red build.
    async 'nitro:config'(nitroConfig) {
      if (nitroConfig.dev) return

      const response = await fetch('https://p.aza.moe/photos')
      if (!response.ok) {
        throw new Error(`Fetching photo manifest for prerendering failed: ${response.status} ${response.statusText}`)
      }
      const photos = await response.json() as Array<{id: string}>
      nitroConfig.prerender ??= {}
      nitroConfig.prerender.routes ??= []
      nitroConfig.prerender.routes.push(...photos.map(photo => `/photo/${photo.id}`))
    },
  },

  compatibilityDate: '2026-08-04',
})
