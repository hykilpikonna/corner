<template>
    <div id="About" class="markdown-content" v-html="html">

    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {marked} from 'marked';
import emojiRegex from 'emoji-regex';

@Options({components: {}})
export default class About extends Vue
{
    html = ""

    mounted(): void
    {
        // TODO: Cloudflare CDN
        fetch("https://raw.githubusercontent.com/hykilpikonna/hykilpikonna/main/README.md").then(it => it.text())
            .then(it => this.html = marked(it.replace(emojiRegex(), (emoji) => {
                return `<span class="emoji">${emoji}</span>`
            })))
    }
}
</script>

<style lang="sass">
@import "../css/colors"

#About
    width: min(600px, 80vw)
    margin: auto
    padding-bottom: 100px
    padding-top: 20px

.emoji
    font-weight: normal

// Markdown style
.markdown-content
    text-align: justify
    text-justify: inter-word

    a
        color: $color-text-special
        text-decoration: none

    h1, h2
        border-bottom: 1px solid $color-text-special
        font-size: 1.5em
        margin-top: 1em

    h1, h2
        line-height: 1.3
        margin-bottom: 0.25em
        padding: 0

    p
        font-size: 0.875em
        margin: 0.5em 0
        line-height: 1.6

    li
        font-size: 0.875em
</style>
