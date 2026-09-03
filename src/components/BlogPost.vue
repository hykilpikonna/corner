<template>
    <Collapse id="BlogPostPreview" class="card tag-top" header-tag="div"
              :active="p.active" @toggle="onToggle">
        <template #header>
            <div id="titles" class="unselectable clickable">
                <div id="date">{{ date.format('YYYY-MM-DD') }}</div>
                <div id="title">{{ meta.title }}</div>
                <div id="subtitle" v-if="meta.subtitle">{{ meta.subtitle }}</div>
                <div class="tags">
                    <div style="display: inline-block">
                        <Tag v-for="t in meta.tags" :key="t" direction="left">{{ t }}</Tag>
                    </div>
                    <i id="pin" class="fas fa-thumbtack" v-if="meta.pinned"></i>
                </div>
            </div>
        </template>

        <div id="content">
            <img class="title-image" :src="p.meta.title_image" v-if="p.meta.title_image" alt="Title Image">
            <div id="text" class="markdown-content">
                <template v-if="p.meta.url_name === 'Index'">
                    <p>按分类检索：</p>
                    <BlogIndex />
                    <p>按主题检索：</p>
                    <BlogIndex mode="categories" />
                </template>
                <div v-else v-html="content"></div>
            </div>
        </div>
    </Collapse>
</template>

<script lang="ts" setup>
import Tag from "@/components/Tag.vue";
import BlogIndex from '@/components/BlogIndex.vue'
import Collapse from '@/components/Collapse.vue'
import type {BlogPost} from "@/scripts/models";
import {useQueryNavigation} from "@/composables/useQueryNavigation";
import {hosts} from "@/scripts/constants";
import {marked} from "marked";
import moment from "moment/moment";
import {computed} from 'vue';

const p = withDefaults(defineProps<{
    meta: BlogPost
    active?: boolean
}>(), {
    active: false
})

const {pushQuery} = useQueryNavigation()

// Header click → update the ?post query; blog.vue recomputes `active`, which
// flows back down through the prop. Collapse toggles optimistically so the UI
// feels instant, and its active watch keeps it in sync on history back/forward.
function onToggle(active: boolean): void
{
    pushQuery({post: active ? p.meta.url_name : null})
}

const content = computed(() => marked.parse(
    p.meta.content.replaceAll('\n', '  \n').replaceAll("{src}", hosts.content),
    {async: false},
).replaceAll('<caption>', '<figcaption>').replaceAll('</caption>', '</figcaption>'))
const date = computed(() => moment(p.meta.date))
</script>

<style lang="sass" scoped>
@use '../css/colors'

#BlogPostPreview
    text-align: left
    display: flex
    flex-direction: column
    overflow: hidden

    #date
        font-size: 0.7em
        color: colors.$color-text-light

    > * + *, #content > * + *
        padding-top: 10px

    .tags
        font-size: 0.7em
        z-index: 50

        #pin
            margin-left: 10px
            transform: rotate(45deg)

    .tag-wrap + .tag-wrap
        margin-left: 5px

    #titles
        // Position patch
        margin: -15px -20px
        padding: 15px 20px

        position: relative

        #title
            font-size: 1.2em
            font-weight: bold

        #subtitle
            font-size: 0.8em
            color: colors.$color-text-light

    img
        $margin: 10px
        max-width: calc(100% + 2 * $margin)
        min-width: calc(100% + 2 * $margin)
        border-radius: 10px
        margin-left: -$margin
        margin-right: -$margin

    // Fix accordion overflow: none
    #content
        $padding: 20px
        margin-left: -$padding
        padding-left: $padding
        margin-right: -$padding
        padding-right: $padding

    #expand
        font-size: 0.8em
        padding-top: 10px
        color: colors.$color-text-light

// Tags positioned over the header
#BlogPostPreview.tag-top
    .tags
        position: absolute
        right: 20px
        top: 15px

@media screen and (max-width: 400px)
    #BlogPostPreview
        img
            $margin: 15px
            max-width: calc(100% + 2 * $margin)
            min-width: calc(100% + 2 * $margin)
            border-radius: 10px
            margin-left: -$margin
            margin-right: -$margin

</style>
