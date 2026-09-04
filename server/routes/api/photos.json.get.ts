// Shared photo-manifest fetch. Prerendered to a static /api/photos.json by
// `nuxt generate`, so the site works on static hosting (no runtime server) while
// all pages read one deduped copy instead of hitting the upstream API each.
// Lives under server/routes (not server/api) so the .json extension is part of
// the URL — gh-pages then serves it with the correct application/json type.
export default defineEventHandler(async () => {
    return await $fetch('https://p.aza.moe/photos')
})
