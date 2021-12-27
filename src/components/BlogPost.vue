<template>
    <div id="BlogPostPreview" class="card" :class="elClass">
        <img class="title-image" :src="image" v-if="image && imageOnTop" alt="Title Image">

        <div id="titles" class="unselectable clickable" @click="clickTitle">
            <div id="date">{{date.format('YYYY-MM-DD')}}</div>
            <div id="title">{{meta.title}}</div>
            <div id="subtitle" v-if="meta.subtitle">{{meta.subtitle}}</div>
            <div class="tags">
                <div v-if="tagOnTop" style="display: inline-block">
                    <Tag v-for="t in meta.tags" :key="t" direction="left">{{t}}</Tag>
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
                <Tag v-for="t in meta.tags" :key="t" direction="right">{{t}}</Tag>
            </div>
        </div>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {Prop, Watch} from "vue-property-decorator";
import {hosts} from "@/scripts/constants";
import {marked} from "marked";
import Tag from "@/components/Tag.vue";
import $ from "jquery";
import 'jqueryui';
import moment from "moment";
import {pushQuery} from "@/scripts/router";

export interface BlogPost
{
    id: number
    title: string
    tags: string[]
    file: string
    date: string
    url_name: string

    content: string

    subtitle?: string
    title_image?: string
    category?: string
    pinned?: number
}

@Options({components: {Tag}})
export default class BlogPostPreview extends Vue
{
    @Prop({required: true}) meta!: BlogPost
    @Prop({default: false}) imageOnTop = false
    @Prop({default: true}) tagOnTop = true
    @Prop({default: false}) active = false

    readonly uid = (Math.random() + 1).toString(36).substring(7)

    isActiveChangeDueToClickTitle = false

    clickTitle(): void
    {
        console.log(`Blog Post: ClickTitle called on`, this.meta.title)
        this.isActiveChangeDueToClickTitle = true

        // Change url
        if (!this.active) pushQuery({post: this.meta.url_name})
        else pushQuery({post: null})
    }

    mounted(): void
    {
        this.updateTitle()

        // Create accordion
        $(`.${this.uid}`).accordion({collapsible: true, header: '#titles', heightStyle: 'content',
            active: this.active ? 0 : false})
    }

    /**
     * Watch active status change, use this to change accordions' activation on history back/forward
     *
     * Also use this to change the title
     */
    @Watch('active')
    onActiveChange(): void
    {
        this.updateTitle()

        // Ignore active status changes due to clicking the title
        console.log('Blog Post: onActiveChange Called on', this.meta.title)
        if (this.isActiveChangeDueToClickTitle)
        {
            this.isActiveChangeDueToClickTitle = false
            return
        }

        // Change accordion activation status
        $(`.${this.uid}`).accordion('option', {active: this.active ? 0 : false});
    }

    updateTitle(): void
    {
        if (this.active) document.title = `Blog: ${this.meta.title}`
    }

    /**
     * Element classes
     */
    get elClass(): string[]
    {
        let classes = [this.uid]
        if (this.imageOnTop) classes.push('image-top')
        if (this.tagOnTop) classes.push('tag-top')
        return classes
    }

    get content(): string { return marked(this.meta.content.replaceAll('\n', '  \n')) }
    get date(): moment.Moment { return moment(this.meta.date) }
    get image(): string | null { return this.meta.title_image ? hosts.content + '/' + this.meta.title_image : null }
}
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
