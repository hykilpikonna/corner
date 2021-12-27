<template>
    <div class="index-tags">
        <Tag v-for="t in meta.tags" :key="t" :tag-name="t[0]" direction="right">{{t[0]}} ({{t[1]}})</Tag>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {BlogMeta, staticMeta} from '@/views/Blog.vue'
import Tag from "@/components/Tag.vue";

@Options({components: {Tag}})
export default class BlogIndexLinks extends Vue
{
    meta: BlogMeta = staticMeta
}
</script>

<style lang="sass" scoped>
.index-tags
    font-size: 0.7em

    * + *
        margin-left: 10px
</style>
