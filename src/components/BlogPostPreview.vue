<template>
    <div id="BlogPostPreview" class="card">
        <div id="date">{{meta.date}}</div>
        <div id="titles">
            <div id="title">{{meta.title}}</div>
            <div id="subtitle" v-if="meta.subtitle">{{meta.subtitle}}</div>
        </div>
        <div id="expand">展开...</div>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {Prop} from "vue-property-decorator";
import {hosts} from "@/scripts/constants";
import {marked} from "marked";

export interface BlogPostMeta
{
    title: string
    tags: string[]
    file: string
    date: string

    subtitle?: string
    title_image?: string
}

@Options({components: {}})
export default class BlogPostPreview extends Vue
{
    @Prop({required: true}) meta!: BlogPostMeta

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
}
</script>

<style lang="sass" scoped>
@import 'src/css/colors'

#BlogPostPreview
    text-align: left

    #date
        font-size: 0.7em
        color: $color-text-light

    #titles
        padding-bottom: 10px

        #title
            font-size: 1.2em
            font-weight: bold

        #subtitle
            font-size: 0.8em
            color: $color-text-light

    #expand
        font-size: 0.8em
        padding-top: 10px

</style>
