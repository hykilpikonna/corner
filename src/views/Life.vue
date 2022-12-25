<template>
    <TgBlog postsUrl="https://profile-api.hydev.org/exports/hykilp/posts.json">
        <div class="title">
            <h2>生活</h2>
            <div class="subtitle">
                <p>
                    在这个时间只会正向流动的世界里，每当秒针划过刻度线，就不可能再回到那里了——每一刻都是我们的第一次、也是最后一次。二十岁，人生中最有趣的这段时光，只匆匆忙忙地经历一次太可惜了。但是或许可以通过这里的照片和文字，让未来的自己能够重新体验这些心情起伏——这是这个频道的初衷。
                </p>
                <p>
                    这里是我记录生活的地方，没有固定格式和内容，更新也会比较频繁。
                </p>
            </div>
        </div>
    </TgBlog>
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
@import "src/css/responsive"

.title
    text-align: left
    margin-bottom: 30px
</style>
