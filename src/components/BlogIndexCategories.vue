<template>
    <div class="index-categories">
        <span v-for="c in meta.categories" :key="c" class="clickable unselectable" @click="click(c)">{{c[0]}} ({{c[1]}})</span>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {BlogMeta, staticMeta} from "@/views/Blog.vue";
import {pushQuery} from "@/scripts/router";

@Options({components: {}})
export default class BlogIndexCategories extends Vue
{
    meta: BlogMeta = staticMeta

    click(c: [string, number]): void
    {
        pushQuery({category: c[0]})
    }
}
</script>

<style lang="sass" scoped>
@import 'src/css/colors'

.index-categories
    font-size: 0.8em
    text-decoration: underline
    color: $color-text-special

    * + *
        margin-left: 10px
</style>
