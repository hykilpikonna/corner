<template>
    <div id="Life" v-if="posts.length !== 0">
        <PostView :p="p" v-for="p in posts" :key="p.id"/>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {backendUrl} from "@/scripts/constants";
import moment from "moment";
import PostView from "@/components/Post.vue";

export interface Image {
    href: string
    url: string
    style: {
        left: number
        top: number
        width: number
        height: number
        'margin-right': number
        'margin-bottom': number
    }
}

export interface Post {
    id: string
    date: string

    text?: string

    type?: string // If type doesn't exist, it's a regular message
    views?: string // Service messages have no view count
    reply?: {
        url: string
        text: string
        thumb?: string
    }
    video?: {
        thumb: string
        duration: string
        src: string
    }
    images?: Image[]
    img_group_style?: {
        width: number
        height: number
    }
}

@Options({components: {PostView}})
export default class Blog extends Vue
{
    posts: Post[] = []

    created(): void
    {
        fetch(`${backendUrl}/posts.json`).then(it => it.json()).then(it => {
            this.posts = it
            this.posts.forEach(it => it.date = moment(it.date).format('YYYY-MM-DD h:mm'))
            this.posts.reverse()
            this.posts = this.posts.filter(it => it.type !== 'service')
            console.log(it)
        })
    }
}
</script>

<style lang="sass" scoped>
@import "src/css/colors"

#Life
    margin: 20px auto
    font-size: 0.8em
    width: min(450px, 80vw)

// Phone layout
@media screen and (max-width: 570px)
    #Life
        margin: 20px 20px 0
        width: unset
</style>
