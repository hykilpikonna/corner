<template>
    <div id="About" class="markdown-content">
        <Dynamic :template="html"></Dynamic>

        <Collapse title="<span class='emoji'>🎓</span> Published papers">
            <ZoteroPublication v-for="item in publications" :key="item.key" :item="item"/>
        </Collapse>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {marked} from 'marked';
import emojiRegex from 'emoji-regex';
import {parseExtensions} from '@/scripts/extended_markdown'
import $ from 'jquery'
import 'jqueryui'
import ZoteroPublication, {ZoteroAttachment, ZoteroItem} from "@/components/ZoteroPublication.vue";

@Options({components: {ZoteroPublication}})
export default class About extends Vue
{
    html = ""
    publications: ZoteroItem[] = []

    mounted(): void
    {
        // TODO: Cloudflare CDN
        fetch("/README.md").then(it => it.text())
            .then(it => this.html = marked(parseExtensions(it.replace(emojiRegex(), (emoji) => {
                return `<span class="emoji">${emoji}</span>`
            }))))

        // Fetch publications from zotero TODO: CDN
        fetch('https://api.zotero.org/users/8463157/publications/items?linkwrap=1&order=date&sort=desc&start=0&include=data&limit=100')
            .then(it => it.json()).then(it =>
            {
                // Filter out publications and attachments
                this.publications = it
                let files: ZoteroAttachment[] = it
                files = files.filter(it => it.data.itemType === 'attachment')
                this.publications = this.publications.filter(it => it.data.itemType !== 'attachment')

                // Add attachments to
                this.publications.forEach(it => it.attachments = files.filter(a => a.data.parentItem == it.key))
            })
    }

    updated(): void
    {
        $('#accordion').accordion({collapsible: true, header: 'h3', heightStyle: "content"})
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

    h3
        font-size: 1.2em

    p
        font-size: 0.9em
        margin: 0.5em 0
        line-height: 1.6

    li
        font-size: 0.9em
        margin-top: 0.25em

</style>
