<template>
    <div id="BlogPostPreview" class="card" :class="elClass">
        <img class="title-image" :src="image" v-if="image && imageOnTop" alt="Title Image">

        <div id="titles" @click="clickTitle">
            <div id="date">{{date.format('YYYY-MM-DD')}}</div>
            <div id="title">{{meta.title}}</div>
            <div id="subtitle" v-if="meta.subtitle">{{meta.subtitle}}</div>
            <div class="tags" v-if="tagOnTop">
                <Tag v-for="t in meta.tags" :key="t" direction="left">{{t}}</Tag>
            </div>
        </div>

        <div id="content">
            <img class="title-image" :src="image" v-if="image && !imageOnTop" alt="Title Image">
            <div id="preview" class="markdown-content" v-html="content"></div>
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
        if (!this.isActive()) this.$router.push(`/blog?post=${this.meta.url_name}`)
        else this.$router.push('/blog')
    }

    mounted(): void
    {
        // Create accordion
        $(`.${this.uid}`).accordion({collapsible: true, header: '#titles', heightStyle: 'content',
            active: this.active ? 0 : false})
    }

    /**
     * Watch active status change, use this to change accordions' activation on history back/forward
     */
    @Watch('active')
    onActiveChange(): void
    {
        console.log('Blog Post: onActiveChange Called on', this.meta.title)

        // Ignore active status changes due to clicking the title
        if (this.isActiveChangeDueToClickTitle)
        {
            this.isActiveChangeDueToClickTitle = false
            return
        }

        // Change accordion activation status
        $(`.${this.uid}`).accordion('option', {active: this.active ? 0 : false});
    }

    isActive(): boolean
    {
        return $(`.${this.uid} > .ui-state-active`).length != 0
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

    get content(): string { return marked(this.meta.content) }
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

    #date
        font-size: 0.7em
        color: $color-text-light

    > * + *, #content > * + *
        padding-top: 10px

    .tags
        font-size: 0.7em

    .tag-wrap + .tag-wrap
        margin-left: 5px

    #titles
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
        right: 0
        top: 0

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
