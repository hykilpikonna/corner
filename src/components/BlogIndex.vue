<template>
    <div class="index index-tags" v-if="mode === 'tags'">
        <Tag v-for="t in meta.tags" :key="t" :tag-name="t[0]" direction="right"
             @click="e => clickTag(e, t)">{{ t[0] }} ({{ t[1] }})</Tag>
    </div>
    <div class="index index-categories" v-else>
        <span v-for="c in meta.categories" :key="c[0]" class="clickable unselectable"
              @click="e => clickCat(e, c)">{{ c[0] }} ({{ c[1] }})</span>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {BlogMeta, staticMeta} from '@/views/Blog.vue'
import Tag from "@/components/Tag.vue";
import {Prop} from "vue-property-decorator";
import {pushQuery} from "@/scripts/router";

@Options({components: {Tag}})
export default class BlogIndexLinks extends Vue
{
    @Prop({default: 'tags'}) mode: 'tags' | 'categories' = 'tags'

    meta: BlogMeta = staticMeta

    clickCat(e: MouseEvent, cat: [string, number]): void
    {
        e.stopPropagation()

        const c = cat[0]
        const q: { [id: string]: string | null } = {category: c}

        // Check if the currently selected post is in this category
        // const url_name = this.$route.query.post
        // if (url_name)
        // {
        //     const posts = staticMeta.posts.filter(it => it.url_name == url_name)
        //     if (posts && posts[0].category != c)
        //     {
        //         // Doesn't include tag, remove post selection
        //         q.post = null
        //     }
        // }

        // Clear tag selection
        q.tag = null

        pushQuery(q)
    }


    clickTag(e: MouseEvent, tag: [string, number]): void
    {
        e.stopPropagation()

        const t = tag[0]
        const q: { [id: string]: string | null } = {tag: t}

        // Check if the currently selected post is in this tag
        // const url_name = this.$route.query.post
        // if (url_name)
        // {
        //     const posts = staticMeta.posts.filter(it => it.url_name == url_name)
        //     if (posts && !posts[0].tags.includes(t))
        //     {
        //         // Doesn't include tag, remove post selection
        //         q.post = null
        //     }
        // }

        // Clear category selection
        q.category = null

        pushQuery(q)
    }
}
</script>

<style lang="sass" scoped>
@import 'src/css/colors'

.index
    *
        display: inline-block
        margin-right: 10px
        margin-bottom: 5px

.index-tags
    font-size: 0.7em

    *
        margin-bottom: 10px

.index-categories
    font-size: 0.8em
    color: $color-text-special

    *
        text-decoration: underline
</style>
