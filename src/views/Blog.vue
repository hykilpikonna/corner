<template>
    <div id="Blog">
        <BlogPostPreview v-for="m of metas" :key="m" :meta="m"/>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import BlogPostPreview, {BlogPostMeta} from "@/components/BlogPostPreview.vue";
import {hosts} from "@/scripts/constants";

@Options({components: {BlogPostPreview}})
export default class Blog extends Vue
{
    metas: BlogPostMeta[] = []

    mounted(): void
    {
        fetch(`${hosts.content}/content/generated/metas.json`).then(it => it.json()).then(it => {
            this.metas = it
        })
    }
}
</script>

<style lang="sass" scoped>
$width: 600px

#Blog
    width: $width
    margin: 20px auto

// Phone layout
@media screen and (max-width: $width + 40px)
    #Blog
        margin: 10px
        width: unset
</style>
