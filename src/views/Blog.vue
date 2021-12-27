<template>
    <div id="Blog">
        <BlogPostPreview v-for="m of filteredPosts" :key="m" :meta="m"
                         :active="post ? m.url_name === post : m === filteredPosts[0] && m.pinned"/>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import BlogPostPreview, {BlogPost} from "@/components/BlogPost.vue";
import {hosts} from "@/scripts/constants";
import {Prop} from "vue-property-decorator";

export interface BlogMeta
{
    tags: [string, number][]
    categories: [string, number][]
    posts: BlogPost[]
}

@Options({components: {BlogPostPreview}})
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
        })
    }

    get filteredPosts(): BlogPost[]
    {
        const posts = this.meta.posts.filter(it => !it.pinned || (this.tag ? it.tags.includes(this.tag) :
            this.category ? it.category == this.category : true))

        // Put pinned posts on top
        posts.sort((a, b) => (b.pinned ?? 0) - (a.pinned ?? 0))

        return posts
    }
}
</script>

<style lang="sass" scoped>
$width: 600px

#Blog
    width: $width
    margin: 20px auto

// Phone layout
@media screen and (max-width: $width + 40px)
    #Blog
        margin: 10px
        width: unset
</style>
