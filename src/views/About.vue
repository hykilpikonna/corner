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
import {hosts} from "@/scripts/constants";

@Options({components: {ZoteroPublication}})
export default class About extends Vue
{
    html = ""
    publications: ZoteroItem[] = []

    mounted(): void
    {
        // Fetch readme
        fetch(`${hosts.content}/README.md`).then(it => it.text())
            .then(it => this.html = marked(parseExtensions(it.replace(emojiRegex(), (emoji) => {
                return `<span class="emoji">${emoji}</span>`
            }))))

        // Fetch publications from zotero
        fetch(`${hosts.api}/zotero.json`)
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

</style>
