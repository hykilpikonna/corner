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
        <BlogPostPreview v-for="m of filteredPosts" :key="m" :meta="m" :active="m === activePost"/>
    </div>
    <Loading v-else></Loading>
</template>

<script setup lang="ts">
import BlogPostPreview from "@/components/BlogPost.vue";
import Loading from "@/components/Loading.vue";
import {BlogMeta} from "@/scripts/models";
import {Ref, ref, computed, onMounted} from "vue";
import {hosts} from "@/scripts/constants";
import {globals} from "@/scripts/global";

const p = defineProps<{
    post?: string,
    category?: string,
    tag?: string
}>()

let meta: Ref<BlogMeta> = ref({tags: [], categories: [], posts: []})

onMounted(() => {
    fetch(`${hosts.content}/content/generated/metas.json`).then(it => it.json()).then(it => {
        meta.value = it
        globals.staticMeta = it
    })
})

const filteredPosts = computed(() => {
    const posts = meta.value.posts.filter(it => it.pinned || (p.tag ? it.tags.includes(p.tag) :
        p.category ? it.category == p.category : true))

    // Put pinned posts on top
    posts.sort((a, b) => (b.pinned ?? 0) - (a.pinned ?? 0))

    return posts
})

const activePost = computed(() => {
    const posts = filteredPosts.value
    if (posts.length == 0) return null
    if (!p.post) return null
    return p.post ? posts.filter(it => it.url_name == p.post)[0] : posts[0].pinned ? posts[0] : null
})
</script>

<style lang="sass" scoped>
@import "src/css/colors"
@import "src/css/responsive"

#breadcrumb
    color: $color-text-light
    margin-bottom: 20px

    span:not(.no-after):after
        content: ">"
        margin: 0 10px

</style>
