<template>
    <div id="Blog" class="general-page" v-if="meta">
        <div class="title">
            <h2>记事本</h2>
            <div class="subtitle">一些值得留下来的文字</div>
        </div>
        <div id="breadcrumb">
            <span class="clickable" @click="() => $router.push({query: {}})">索引</span>
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
import {BlogMeta} from "@/scripts/models";
import {Ref, ref, computed, onMounted} from "vue";
import metasJson from "@/data/blog/metas.json";
import {globals} from "@/scripts/global";
import {Router} from "vue-router";

let $router: Router

const p = defineProps<{
    post?: string,
    category?: string,
    tag?: string
}>()

let meta: Ref<BlogMeta> = ref(metasJson as unknown as BlogMeta)

const query = ref({
    post: p.post,
    category: p.category,
    tag: p.tag
})

onMounted(() => {
    globals.staticMeta = meta.value
    
    const initQuery = () => {
        const url = new URL(window.location.href);
        query.value = {
            post: url.searchParams.get('post') || undefined,
            category: url.searchParams.get('category') || undefined,
            tag: url.searchParams.get('tag') || undefined
        }
    }
    
    initQuery()
    window.addEventListener('popstate', initQuery)
})

const filteredPosts = computed(() => {
    const posts = meta.value.posts.filter(it => it.pinned || (query.value.tag ? it.tags.includes(query.value.tag) :
        query.value.category ? it.category == query.value.category : true))

    // Put pinned posts on top
    posts.sort((a, b) => (b.pinned ?? 0) - (a.pinned ?? 0))

    return posts
})

const activePost = computed(() => {
    const posts = filteredPosts.value
    if (posts.length == 0) return null
    if (!query.value.post) return null
    return posts.filter(it => it.url_name == query.value.post)[0]
})
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
