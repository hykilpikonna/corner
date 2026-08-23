// Shared photo-manifest fetch. Prerendered to a static /api/photos JSON file by
// `nuxt generate`, so the site works on static hosting (no runtime server) while
// all pages read one deduped copy instead of hitting the upstream API each.
export default defineEventHandler(async () => {
    return await $fetch('https://p.aza.moe/photos')
})
