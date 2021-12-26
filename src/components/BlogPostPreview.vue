<template>
    <div id="BlogPostPreview" class="card" :class="{'image-top': imageOnTop, 'tag-top': tagOnTop}">
        <div id="titles">
            <div id="date">{{meta.date}}</div>
            <div id="title">{{meta.title}}</div>
            <div id="subtitle" v-if="meta.subtitle">{{meta.subtitle}}</div>
            <div class="tags" v-if="tagOnTop">
                <Tag v-for="t in meta.tags" :key="t" direction="left">{{t}}</Tag>
            </div>
        </div>
        <img id="title-image" :src="image" v-if="image" alt="Title Image">
        <div id="preview" class="markdown-content" v-html="content"></div>
        <div class="tags" v-if="!tagOnTop">
            <Tag v-for="t in meta.tags" :key="t" direction="right">{{t}}</Tag>
        </div>
<!--        <div id="expand" v-if="meta.more_content">展开...</div>-->
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {Prop} from "vue-property-decorator";
import {hosts} from "@/scripts/constants";
import {marked} from "marked";
import Tag from "@/components/Tag.vue";

export interface BlogPostMeta
{
    title: string
    tags: string[]
    file: string
    date: string

    content: string

    subtitle?: string
    title_image?: string
}

@Options({components: {Tag}})
export default class BlogPostPreview extends Vue
{
    @Prop({required: true}) meta!: BlogPostMeta
    @Prop({default: true}) imageOnTop = true
    @Prop({default: false}) tagOnTop = false

    created(): void
    {
        if (!this.meta.subtitle)
        {
            // fetch(`${hosts.content}/${this.meta.file}`).then(it => it.text()).then(it =>
            // {
            //     this.meta.subtitle = marked(it)
            // })
        }
    }

    get content(): string { return marked(this.meta.content) }

    get image(): string | null
    { return this.meta.title_image ? hosts.content + '/' + this.meta.title_image : null }
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

    > * + *
        margin-top: 10px

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

    #expand
        font-size: 0.8em
        padding-top: 10px
        color: $color-text-light

// Put image on top
#BlogPostPreview.image-top
    #title-image
        order: -1
        margin: -15px -20px 10px
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
