<template>
  <div id="Friends" class="general-page">
    <div class="title">
      <h2>朋友们</h2>
      <div class="subtitle">
        是小桂桂的朋友们（欢迎补充<br>
        每次刷新都会打乱顺序！
      </div>
    </div>

    <div class="friends" v-if="friends">
      <div class="friend card" v-for="f in friends" :key="f.name">
        <div class="banner" :style="bgStyle(f)"></div>
        <img class="avatar" :src="f.avatar" alt="">
        <div class="info">
          <div class="name unselectable">{{ f.name }}</div>
          <div class="links">
            <a v-for="l in getFriendLinks(f)" :href="l.link"><i :class="l.icon"></i></a>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import {onMounted, ref} from 'vue'
import {fab, hosts} from "@/scripts/constants";
import {shuffle} from "@/scripts/utils";

interface Friend {
  name: string
  avatar: string
  banner: string
  desc?: string
  [id: string]: string | undefined
}

const excludes = new Set(["name", "avatar", "banner", "desc"])
const icons: {[id: string]: string} = {
  blog: 'fas fa-book'
}

const friends = ref<Friend[]>([])

const bgStyle = (f: Friend) => {
  if (f.banner) return {'background-image': `url("${f.banner}")`}
  return {}
}

const getFriendLinks = (f: Friend): { link: string, icon: string }[] => {
  return Object.entries(f)
    .filter(([key, value]) => !excludes.has(key) && typeof value === 'string')
    .map(([key, value]) => ({
      link: value,
      icon: fab.includes(key) ? `fab fa-${key}` : (key in icons ? icons[key] : key)
    }))
}

onMounted(async () => {
  friends.value = await (await fetch(`${hosts.content}/content/generated/friends/friends.json`)).json()

  friends.value.forEach(f => {
    if (!f.avatar.startsWith('http')) f.avatar = `${hosts.content}/${f.avatar}`
    if (f.banner && !f.banner.startsWith('http')) f.banner = `${hosts.content}/${f.banner}`
  })
  friends.value = shuffle(friends.value)
})
</script>

<style lang="sass" scoped>
@use "../../css/colors"
@use "../../css/responsive"

$card-min-width: 320px

//.friends
//  display: grid
//  grid-template-columns: repeat(auto-fit, minmax($card-min-width, 1fr))
//  gap: 20px

.friend
  display: flex
  position: relative
  min-width: $card-min-width

  $top: calc(100px + max(min(100vw, 600px), $card-min-width + 20px * 2) * 0.1 - $card-min-width * 0.1)
  $img: 80px

  .banner
    position: absolute
    left: 0
    top: 0
    z-index: 1
    width: 100%
    height: $top
    background-color: rgb(166 134 89 / 34%)
    background-size: cover
    background-position: center

  .banner:after
    content: " "
    position: absolute
    z-index: 2
    width: 100%
    height: 100%
    background: linear-gradient(0deg, rgba(0, 0, 0, 0.15) 0%, rgba(0, 0, 0, 0) 50%)

  .info
    z-index: 10
    display: flex
    align-items: end
    width: 100%

    .name
      font-size: 1.2em
      white-space: nowrap
      overflow: hidden
      flex: 1

    a
      color: colors.$color-text-main

    a + a
      margin-left: 10px

  .avatar
    margin-top: calc(#{$top} - #{$img} / 2 - 20px)
    width: $img
    height: $img
    object-fit: contain
    border-radius: 100%
    margin-right: 20px
    z-index: 10
</style>
