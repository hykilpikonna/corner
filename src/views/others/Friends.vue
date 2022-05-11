<template>
  <div id="Friends" class="general-page">
    <div class="title">
      <h2>朋友们</h2>
      <div class="subtitle">是小桂桂的朋友们</div>
    </div>

    <div class="friends" v-if="friends">
      <div class="friend card clickable" v-for="f in friends" :key="f.name" @click="click(f)">
        <img class="avatar" :src="f.avatar" alt="">
        <div class="info">
          <div class="name">{{ f.name }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {hosts} from "@/scripts/constants";

export interface Friend {
  name: string
  link: string
  avatar: string
  banner: string

  desc?: string
}

@Options({components: {}})
export default class Friends extends Vue
{
  friends: Friend[] = []

  async created()
  {
    this.friends = await (await fetch(`${hosts.content}/content/generated/friends/friends.json`)).json()

    // Fix avatar relative url
    this.friends.forEach(f => {
      if (!f.avatar.startsWith('http')) f.avatar = `${hosts.content}/${f.avatar}`
    })
  }

  click(f: Friend)
  {
    window.open(f.link)
  }
}
</script>

<style lang="sass" scoped>
@import "src/css/colors"
@import "src/css/responsive"
</style>
