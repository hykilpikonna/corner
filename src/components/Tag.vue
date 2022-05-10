<template>
    <div class="tag-wrap unselectable clickable" @click="e => clickTag(e)">
        <div class="tag fbox-vcenter" :class="direction">
            <div ref="el"><slot></slot></div>
            <div class="after"></div>
        </div>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {Prop, Ref} from "vue-property-decorator";
import {pushQuery} from "@/scripts/router";
import {staticMeta} from '@/views/Blog.vue'

@Options({components: {}})
export default class Tag extends Vue
{
    @Prop({default: 'left'}) direction: 'left' | 'right' = 'left'
    @Prop() tagName?: string

    @Ref() readonly el!: HTMLDivElement

    clickTag(e: MouseEvent): void
    {
        e.stopPropagation()

        const t = this.tagName ?? this.el.innerText
        const q: {[id: string]: string | null} = {tag: t}

        // Check if the currently selected post is in this tag
        const url_name = this.$route.query.post
        if (url_name)
        {
            const posts = staticMeta.posts.filter(it => it.url_name == url_name)
            if (posts && !posts[0].tags.includes(t))
            {
                // Doesn't include tag, remove post selection
                q.post = null
            }
        }

        pushQuery(q)
    }
}
</script>

<style lang="sass" scoped>
@import src/css/colors

$tag-height: 20px
$tag-color: $color-bg-6
$text-color: $color-text-light

$padding: calc($tag-height / 2)
$triangle-width: calc($tag-height / 2) * 0.8

.tag-wrap
    display: inline-block

.tag
    background: $tag-color
    color: $text-color
    display: inline-flex
    height: $tag-height
    position: relative
    transition: color 0.2s

    .after
        border-bottom: $padding solid transparent
        border-top: $padding solid transparent
        border-left: $triangle-width solid $tag-color
        position: absolute
        top: 0

.tag.right
    padding: 0 ($padding - 4) 0 $padding
    margin-right: $triangle-width
    border-radius: 3px 0 0 3px

    .after
        right: -$triangle-width

.tag.left
    padding: 0 $padding 0 ($padding - 4)
    margin-left: $triangle-width
    border-radius: 0 3px 3px 0

    .after
        transform: rotate(180deg)
        left: -$triangle-width
</style>
