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

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import BlogPostPreview, {BlogPost} from "@/components/BlogPost.vue";
import {hosts} from "@/scripts/constants";
import {Prop} from "vue-property-decorator";
import Loading from "@/components/Loading.vue";

export interface BlogMeta
{
    // tags[i] = [Tag Name, Number of Blog Posts]
    tags: [string, number][]
    // categories[i] = [Category Name, Number of Posts]
    categories: [string, number][]
    posts: BlogPost[]
}

export let staticMeta: BlogMeta = {tags: [], categories: [], posts: []}

@Options({components: {Loading, BlogPostPreview}})
export default class Blog extends Vue
{
    @Prop() post?: string
    @Prop() category?: string
    @Prop() tag?: string

    meta: BlogMeta = {tags: [], categories: [], posts: []}

    mounted(): void
    {
        fetch(`${hosts.content}/content/generated/metas.json`).then(it => it.json()).then(it => {
            this.meta = it
            staticMeta = it
        })
    }

    get activePost(): BlogPost | null
    {
        const p = this.filteredPosts
        if (p.length == 0) return null
        return this.post ? p.filter(it => it.url_name == this.post)[0] : p[0].pinned ? p[0] : null
    }

    get filteredPosts(): BlogPost[]
    {
        const posts = this.meta.posts.filter(it => it.pinned || (this.tag ? it.tags.includes(this.tag) :
            this.category ? it.category == this.category : true))

        // Put pinned posts on top
        posts.sort((a, b) => (b.pinned ?? 0) - (a.pinned ?? 0))

        return posts
    }
}
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
