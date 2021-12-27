<template>
    <div class="index-tags">
        <div v-for="t in meta.tags" :key="t">{{t}}</div>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {BlogMeta, staticMeta} from '@/views/Blog.vue'

@Options({components: {}})
export default class BlogIndexLinks extends Vue
{
    meta: BlogMeta = staticMeta
}
</script>

<style lang="sass" scoped>

</style>
