<template>
    <div id="BlogPostPreview" class="card" :class="elClass">
        <img class="title-image" :src="image" v-if="image && imageOnTop" alt="Title Image">

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
            <img class="title-image" :src="image" v-if="image && !imageOnTop" alt="Title Image">
            <div id="text" class="markdown-content">
                <Dynamic :template="content"></Dynamic>
            </div>
            <div class="tags" v-if="!tagOnTop">
                <Tag v-for="t in meta.tags" :key="t[0]" direction="right">{{ t }}</Tag>
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
import moment from "moment/moment";
import {computed, onMounted, watch} from 'vue';

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

const uid = (Math.random() + 1).toString(36).substring(7)

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
    updateTitle()

    // Create accordion
    $(`.${uid}`).accordion({
        collapsible: true, header: '#titles', heightStyle: 'content',
        active: p.active ? 0 : false
    })
})

/**
 * Watch active status change, use this to change accordions' activation on history back/forward
 *
 * Also use this to change the title
 */
watch(() => p.active, (active, _) => {
    updateTitle()

    // Ignore active status changes due to clicking the title
    console.log('Blog Post: onActiveChange Called on', p.meta.title)
    if (isActiveChangeDueToClickTitle)
    {
        isActiveChangeDueToClickTitle = false
        return
    }

    // Change accordion activation status
    $(`.${uid}`).accordion('option', {active: active ? 0 : false});
})

function updateTitle(): void
{
    if (p.active) document.title = `Blog: ${p.meta.title}`
}

/**
 * Element classes
 */
const elClass = computed(() =>
{
    let classes = [uid]
    if (p.imageOnTop) classes.push('image-top')
    if (p.tagOnTop) classes.push('tag-top')
    return classes
})

const content = computed(() => marked(p.meta.content.replaceAll('\n', '  \n')))
const date = moment(p.meta.date)
const image = p.meta.title_image ? hosts.content + '/' + p.meta.title_image : null
</script>

<style lang="sass" scoped>
@import 'src/css/colors'

#BlogPostPreview
    text-align: left
    display: flex
    flex-direction: column
    overflow: hidden

    #date
        font-size: 0.7em
        color: $color-text-light

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
            color: $color-text-light

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
        color: $color-text-light

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
