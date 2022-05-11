<template>
  <div id="Others">
    <h2>更多链接</h2>
    <router-link class="rlink" to="/kitchen-menu">🍳 小桂桂的私房菜 菜单</router-link>
    <router-link class="rlink" to="/friends">🎎 朋友们</router-link>
  </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import Collapse from "@/components/Collapse.vue";

@Options({components: {Collapse}})
export default class Others extends Vue
{


}
</script>

<style lang="sass" scoped>
@import "src/css/colors"

#Others
  text-align: left
  width: 600px
  margin: 2em auto 0

a
  color: $color-text-main
  display: block
  margin-bottom: 0.5em
</style>
