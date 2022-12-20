<template>
    <div id="Life">
        <div id="header">
            <p>欢迎！</p>
            <p>
                在这个时间只会正向流动的世界里，每当秒针划过刻度线，就不可能再回到那里了——每一刻都是我们的第一次、也是最后一次。二十岁，人生中最有趣的这段时光，只匆匆忙忙地经历一次太可惜了。但是或许可以通过这里的照片和文字，让未来的自己能够重新体验这些心情起伏——这是这个频道的初衷。
            </p>
            <p>
                这里是我记录生活的地方，没有固定格式内容，更新也会比较频繁。
            </p>
        </div>
        <TgBlog postsUrl="https://test-tg-data.hydev.org/liaotalk/posts.json"></TgBlog>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import { TgBlog } from "tg-blog";
import 'tg-blog/dist/style.css'

@Options({components: {
    TgBlog
}})
export default class Blog extends Vue
{
}
</script>

<style lang="sass" scoped>
@import "src/css/colors"

#Life
    margin: 20px auto
    //font-size: 0.92em
    width: min(450px, 80vw)

    #header
        color: $color-text-light
        margin-bottom: 30px

// Phone layout
@media screen and (max-width: 570px)
    #Life
        margin: 20px 20px 0
        width: unset
</style>
