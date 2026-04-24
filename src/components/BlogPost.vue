<template>
    <div id="BlogPostPreview" class="card" :class="elClass">
        <img class="title-image" :src="p.meta.title_image" v-if="p.meta.title_image && imageOnTop" alt="Title Image">

        <div id="titles" class="unselectable clickable" @click="clickTitle">
            <div id="date">{{ date.format('YYYY-MM-DD') }}</div>
            <div id="title">{{ p.meta.title }}</div>
            <div id="subtitle" v-if="p.meta.subtitle">{{ p.meta.subtitle }}</div>
            <div class="tags">
                <div v-if="tagOnTop" style="display: inline-block">
                    <Tag v-for="t in p.meta.tags" :key="t" direction="left">{{ t }}</Tag>
                </div>
                <i id="pin" class="fas fa-thumbtack" v-if="p.meta.pinned"></i>
            </div>
        </div>

        <div id="content">
            <img class="title-image" :src="p.meta.title_image" v-if="p.meta.title_image && !imageOnTop" alt="Title Image">
            <div id="text" class="markdown-content" v-html="content" ref="textRef">
            </div>
            <div class="tags" v-if="!tagOnTop">
                <Tag v-for="t in p.meta.tags" :key="t[0]" direction="right">{{ t }}</Tag>
            </div>
        </div>
    </div>
</template>

<script lang="ts" setup>
import Tag from "@/components/Tag.vue";
import {BlogPost} from "@/scripts/models";
import {pushQuery} from "@/scripts/router";
import {$, hosts} from "@/scripts/constants";
import {marked} from "marked";
import moment from "moment";
import {computed, onMounted, watch, ref, nextTick, createApp} from 'vue';
import BlogIndex from './BlogIndex.vue';

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

const uid = 'bp-' + p.meta.url_name.replace(/[^a-z0-9]/gi, '-')
const textRef = ref<HTMLElement | null>(null)

let isActiveChangeDueToClickTitle = false

function clickTitle(): void
{
    console.log(`Blog Post: ClickTitle called on`, p.meta.title)
    isActiveChangeDueToClickTitle = true

    // Change url
    if (!p.active) pushQuery({post: p.meta.url_name})
    else pushQuery({post: null})
}

const mountComponents = () => {
    if (!textRef.value) return
    
    // Handle BlogIndex
    textRef.value.querySelectorAll('blogindex').forEach(el => {
        const mode = el.getAttribute('mode') as any || 'tags'
        const app = createApp(BlogIndex, { mode })
        app.mount(el)
    })

    // Fix image heights from attributes
    textRef.value.querySelectorAll('img[height]').forEach(img => {
        const h = img.getAttribute('height');
        if (h) (img as HTMLElement).style.height = h.includes('px') ? h : h + 'px';
    });
}

onMounted(() => {
    updateTitle()

    // Create accordion
    $(`.${uid}`).accordion({
        collapsible: true, header: '#titles', heightStyle: 'content',
        active: p.active ? 0 : false
    })
    
    mountComponents()
})

watch(() => p.active, (active, _) => {
    updateTitle()

    console.log('Blog Post: onActiveChange Called on', p.meta.title, 'active:', active)
    $(`.${uid}`).accordion('option', {active: active ? 0 : false});
})

watch(() => p.meta.content, () => {
    nextTick(mountComponents)
})

function updateTitle(): void
{
    if (p.active) document.title = `Blog: ${p.meta.title}`
}

const elClass = computed(() =>
{
    let classes = [uid]
    if (p.imageOnTop) classes.push('image-top')
    if (p.tagOnTop) classes.push('tag-top')
    return classes
})

const content = computed(() => {
    let raw = p.meta.content.replaceAll('\n', '  \n').replaceAll("{src}", hosts.content)
    let html = marked.parse(raw) as string
    
    // Handle Obsidian-style images ![[url|caption]]
    html = html.replace(/!\[\[(.*?)\]\]/g, (match, content) => {
        const [url, caption] = content.split('|')
        const fullUrl = url.startsWith('http') ? url : `${hosts.content}/posts/${url}`
        if (caption) {
            return `<figure class="image-wrap"><img src="${fullUrl}" alt="${caption}"><caption class="caption">${caption}</caption></figure>`
        }
        return `<img src="${fullUrl}">`
    })

    return html
})
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

    .image-wrap
        margin: 1em 0
        display: flex
        flex-direction: column
        align-items: center

        img
            margin-left: 0
            margin-right: 0
            width: 100%

    .caption, caption
        display: block
        width: 100%
        text-align: center
        margin-top: 0.5em
        font-size: 1.1em
        color: colors.$color-text-light
        font-family: 'Caveat', 'Shadows Into Light', cursive
        opacity: 0.8

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
