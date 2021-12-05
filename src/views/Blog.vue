<template>
    <div id="Blog" v-if="posts.length !== 0">
        <div class="post" v-for="p in posts" :key="p.id" :class="{'service': p.type === 'service'}">
            <div class="reply" v-if="p.reply">
                <img class="thumb" v-if="p.reply.thumb" :src="p.reply.thumb" alt="">
                <div class="mtext">{{p.reply.text}}</div>
            </div>
            <div class="images" v-if="p.images">
                <div class="img" v-for="i in p.images" :key="i"
                     :style="{'background-image': `url(${i.url})`}"></div>
            </div>
            <div class="text">{{p.text}}</div>
            <div class="info font-code">
                <div class="id">#{{p.id}}</div>
                <div class="f-grow1"></div>
                <div class="date">{{p.date}}</div>
                <div class="views" v-if="p.views">{{p.views}}<i class="fas fa-eye"/></div>
            </div>
        </div>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {backendUrl} from "@/constants";
import moment from "moment";

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
    images?: {
        href: string
        url: string
    }[]
}

@Options({components: {}})
export default class Blog extends Vue
{
    posts: Post[] = []

    created(): void
    {
        fetch(`${backendUrl}/posts.json`).then(it => it.json()).then(it => {
            this.posts = it
            this.posts.forEach(it => it.date = moment(it.date).format('YYYY-MM-DD h:mm'))
            this.posts.reverse()
            console.log(it)
        })
    }
}
</script>

<style lang="sass" scoped>
@import "src/css/colors"

#Blog
    background: #fdf9f1
    margin: 20px auto 0
    border-radius: 50px 50px 0 0
    padding: 20px
    font-size: 0.9em
    max-width: 600px

.post.service
    .id
        display: none

.post
    width: 100%
    background: #f9f2e0
    border-radius: 20px
    margin-bottom: 20px
    padding-bottom: 8px

    .text
        white-space: pre-line

    .info
        display: flex
        color: lighten($color-text-main, 40)
        div
            margin: 0 10px

        .id
            margin-left: 20px

        .views
            margin-right: 20px

        i
            font-size: 0.8em
            margin-left: 4px
</style>
