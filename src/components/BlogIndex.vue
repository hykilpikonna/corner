<template>
    <div class="index index-tags" v-if="mode === 'tags'">
        <Tag v-for="t in meta.tags" :key="t" :tag-name="t[0]" direction="right">{{t[0]}} ({{t[1]}})</Tag>
    </div>
    <div class="index index-categories" v-else>
        <span v-for="c in meta.categories" :key="c" class="clickable unselectable" @click="click(c)">{{c[0]}} ({{c[1]}})</span>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {BlogMeta, staticMeta} from '@/views/Blog.vue'
import Tag from "@/components/Tag.vue";
import {Prop} from "vue-property-decorator";
import {pushQuery} from "@/scripts/router";

@Options({components: {Tag}})
export default class BlogIndexLinks extends Vue
{
    @Prop({default: 'tags'}) mode: 'tags' | 'categories' = 'tags'

    meta: BlogMeta = staticMeta

    click(c: [string, number]): void
    {
        pushQuery({category: c[0]})
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
