<template>
    <div id="BlogPostPreview" ref="postElement" class="card" :class="elClass">
        <img class="title-image" :src="p.meta.title_image" v-if="p.meta.title_image && imageOnTop" alt="Title Image">

        <div id="titles" class="unselectable clickable" @click="clickTitle">
            <div id="date">{{ date.format('YYYY-MM-DD') }}</div>
            <div id="title">{{ meta.title }}</div>
            <div id="subtitle" v-if="meta.subtitle">{{ meta.subtitle }}</div>
            <div class="tags">
                <div v-if="tagOnTop" style="display: inline-block">
                    <Tag v-for="t in meta.tags" :key="t" direction="left">{{ t }}</Tag>
                </div>
                <i id="pin" class="fas fa-thumbtack" v-if="meta.pinned"></i>
            </div>
        </div>

        <div id="content">
            <img class="title-image" :src="p.meta.title_image" v-if="p.meta.title_image && !imageOnTop" alt="Title Image">
            <div id="text" class="markdown-content">
                <template v-if="p.meta.url_name === 'Index'">
                    <p>按分类检索：</p>
                    <BlogIndex />
                    <p>按主题检索：</p>
                    <BlogIndex mode="categories" />
                </template>
                <div v-else v-html="content"></div>
            </div>
            <div class="tags" v-if="!tagOnTop">
                <Tag v-for="t in meta.tags" :key="t[0]" direction="right">{{ t }}</Tag>
            </div>
        </div>
    </div>
</template>

<script lang="ts" setup>
import Tag from "@/components/Tag.vue";
import BlogIndex from '@/components/BlogIndex.vue'
import type {BlogPost} from "@/scripts/models";
import {useQueryNavigation} from "@/composables/useQueryNavigation";
import {$, hosts} from "@/scripts/constants";
import {marked} from "marked";
import moment from "moment/moment";
import {computed, onMounted, ref, watch} from 'vue';

const p = withDefaults(defineProps<{
    meta: BlogPost
    imageOnTop?: boolean
    tagOnTop?: boolean
    active?: boolean
}>(), {
    imageOnTop: false,
    tagOnTop: true,
    active: false
})

const {pushQuery} = useQueryNavigation()

const postElement = ref<HTMLElement | null>(null)

let isActiveChangeDueToClickTitle = false

function clickTitle(): void
{
    console.log(`Blog Post: ClickTitle called on`, p.meta.title)
    isActiveChangeDueToClickTitle = true

    // Change url
    if (!p.active) pushQuery({post: p.meta.url_name})
    else pushQuery({post: null})
}

onMounted(() => {
    // Create accordion
    $(postElement.value).accordion({
        collapsible: true, header: '#titles', heightStyle: 'content',
        active: p.active ? 0 : false
    })
})

/**
 * Watch active status change, use this to change accordions' activation on history back/forward
 */
watch(() => p.active, (active, _) => {
    // Ignore active status changes due to clicking the title
    console.log('Blog Post: onActiveChange Called on', p.meta.title)
    if (isActiveChangeDueToClickTitle)
    {
        isActiveChangeDueToClickTitle = false
        return
    }

    // Change accordion activation status
    $(postElement.value).accordion('option', {active: active ? 0 : false});
})

/**
 * Element classes
 */
const elClass = computed(() =>
{
    let classes: string[] = []
    if (p.imageOnTop) classes.push('image-top')
    if (p.tagOnTop) classes.push('tag-top')
    return classes
})

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

// Put image on top
#BlogPostPreview.image-top
    .title-image
        margin: -15px -20px 0px
        max-width: calc(100% + 40px)
        min-width: calc(100% + 40px)

// Put tags on top
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
