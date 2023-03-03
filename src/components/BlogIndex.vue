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
import {staticMeta} from '@/views/Blog.vue'
import Tag from "@/components/Tag.vue";
import {Prop} from "vue-property-decorator";
import {pushQuery} from "@/scripts/router";
import {BlogMeta} from "@/scripts/models";

@Options({components: {Tag}})
export default class BlogIndexLinks extends Vue
{
    @Prop({default: 'tags'}) mode: 'tags' | 'categories' = 'tags'

    meta: BlogMeta = staticMeta

    clickCat(e: MouseEvent, cat: [string, number]): void
    {
        e.stopPropagation()
        pushQuery({category: cat[0], tag: null})
    }


    clickTag(e: MouseEvent, tag: [string, number]): void
    {
        e.stopPropagation()
        pushQuery({tag: tag[0], category: null})
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
