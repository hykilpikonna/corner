<template>
    <div id="Others" class="general-page">
        <div class="title">
            <h2>更多链接</h2>
            <div class="subtitle">欢迎点进来看看</div>
        </div>
        <div class="links">
            <router-link class="rlink" to="/kitchen-menu">🍳 小桂桂的私房菜 菜单</router-link>
            <router-link class="rlink" to="/friends">🎎 朋友们</router-link>
            <a href="https://cast.hydev.org">📹 公开直播间</a>
            <a href="http://status.hydev.org/">🔌 看看服务器们过得怎么样</a>
        </div>
    </div>
</template>

<style lang="sass" scoped>
@import "src/css/global"
@import "src/css/colors"
@import "src/css/responsive"

.links
    display: flex
    gap: 10px
    flex-direction: column
    align-items: flex-start

a
    @extend .card
    @extend .clickable

    color: $color-text-main
    text-decoration: none

</style>
