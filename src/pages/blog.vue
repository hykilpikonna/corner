<template>
    <div id="Blog" class="general-page" v-if="meta">
        <div class="title">
            <h2>记事本</h2>
            <div class="subtitle">一些值得留下来的文字</div>
        </div>
        <div id="breadcrumb">
            <span class="clickable" @click="clearQuery">索引</span>
            <span v-if="tag">🏷️{{tag}}</span>
            <span v-if="category">📂{{category}}</span>
            <span class="no-after" v-if="post && activePost">{{activePost.title}}</span>
        </div>
        <BlogPostPreview v-for="m of filteredPosts" :key="m.id" :meta="m" :active="m === activePost"/>
    </div>
    <Loading v-else></Loading>
</template>

<script setup lang="ts">
import BlogPostPreview from "@/components/BlogPost.vue";
import Loading from "@/components/Loading.vue";
import {computed, onMounted, provide} from "vue";
import type {BlogMeta} from "@/scripts/models";
import {hosts} from "@/scripts/constants";
import {blogMetaKey, emptyBlogMeta} from "@/scripts/global";

definePageMeta({title: '记事本', navBookmark: 'blog'})

const route = useRoute()
const router = useRouter()

const {data: meta, refresh: refreshMeta} = await useFetch<BlogMeta>(`${hosts.content}/content/generated/metas.json`, {
    key: 'blog-meta',
    default: emptyBlogMeta,
})

provide(blogMetaKey, meta)
onMounted(() => void refreshMeta())

const post = computed(() => typeof route.query.post === 'string' ? route.query.post : undefined)
const category = computed(() => typeof route.query.category === 'string' ? route.query.category : undefined)
const tag = computed(() => typeof route.query.tag === 'string' ? route.query.tag : undefined)

const clearQuery = () => router.push({query: {}})

const filteredPosts = computed(() => {
    const posts = meta.value.posts.filter(it => it.pinned || (tag.value ? it.tags.includes(tag.value) :
        category.value ? it.category == category.value : true))

    // Put pinned posts on top
    posts.sort((a, b) => (b.pinned ?? 0) - (a.pinned ?? 0))

    return posts
})

const activePost = computed(() => {
    const posts = filteredPosts.value
    if (posts.length == 0) return null
    if (!post.value) return null
    return posts.find(it => it.url_name === post.value) ?? null
})

useHead(() => ({
    title: activePost.value ? `Blog: ${activePost.value.title}` : 'Aza - 记事本'
}))
</script>

<style lang="sass" scoped>
@use "../css/colors"
@use "../css/responsive"

#breadcrumb
    color: colors.$color-text-light
    margin-bottom: 20px

    span:not(.no-after):after
        content: ">"
        margin: 0 10px

</style>
