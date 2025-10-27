<script lang="ts">
import { Vue, Component } from 'vue-facing-decorator';

interface PhotoMetadata {
  id: string
  owner_key: string // This will be filtered out for public requests
  upload_time: string
  original_photo: string
  edited_photo: string
  thumbnail: string
  thumbnail_edited: string
  exif: {[id: string]: string}
}

// Take in a string, use its hash to produce a number from 0 to 1
function detRandom(seed: string): number {
  return Array.from(seed).reduce((acc, char) => (acc + char.charCodeAt(0) * 65535) % 22859, 0) / 22859
}

@Component({})
export default class Photos extends Vue {
  photos: PhotoMetadata[]
  photoRows: PhotoMetadata[][]

  async created() {
    this.photos = await (await fetch('https://p.aza.moe/photos')).json()
    this.photos.sort((a, b) => (a.exif.DateTime < b.exif.DateTime ? 1 : -1))

    let rowProbabilityTable = {
      1: 0, 2: 0.3, 3: 0.5
    }

    // Generate photo rows: there is a 10% chance that a photo will be the only photo in its row
    this.photoRows = []
    let currentRow: PhotoMetadata[] = []
    this.photos.forEach((p) => {
      if (currentRow.length === 0) currentRow.push(p)
      else if (currentRow.length >= 3) {
        this.photoRows.push(currentRow)
        currentRow = [ p ]
      }
      else {
        const singleChance = detRandom(p.original_photo)
        if (singleChance < rowProbabilityTable[currentRow.length]) {
          this.photoRows.push(currentRow)
          currentRow = [ p ]
        } else currentRow.push(p)
      }
    })
    if (currentRow.length > 0) this.photoRows.push(currentRow)
  }

  url(s: string): string {
    s = s.replace('data/photos', 'static').replace('./', '')
    return `https://p.aza.moe/${s}`
  }

  randomRotation(s: string): string {
    const angle = (detRandom(s) * 20) - 10 // -10 to +10 degrees
    return `rotate(${angle}deg)`
  }

  async clickPhoto(p: PhotoMetadata, e: MouseEvent) {
    console.log("Clicked photo:", p.id)
    const dom = e.currentTarget as HTMLDivElement
    const photoEl = dom.querySelector('.photo-wrapper') as HTMLDivElement

    photoEl.style.viewTransitionName = `photo-${p.id}`

    const transition = document.startViewTransition(() => {
      dom.classList.toggle('active')
      document.getElementsByClassName('blur')[0].toggleAttribute('hidden')
    })

    await transition.finished
    photoEl.style.viewTransitionName = ''
  }
}
</script>

<template>
  <div class="title">
    <div class="font-script-en bold">The Wandering Gallery</div>
    <div class="subtitle <sm:hidden">想要把旅行中用相机拍到好看照片时的喜悦分享给照片里出现的路人，所以买了便携照片打印机、搭了这个网页！</div>
  </div>
  <div class="outer-grid">
    <div v-for="row in photoRows" :key="row[0].id" flex justify-center :class="`grid-cols-${row.length}`">
      <div v-for="p in row" :key="p.id" @click.capture="async e => await clickPhoto(p, e)"
           class="img-container" cursor-pointer>
        <img class="photo" w-full h-full object-contain opacity-0 :src="url(p.thumbnail_edited)" :alt="p.id"/>
        <div class="photo-abs-container" absolute inset-0 flex justify-center items-center>
          <div class="photo-wrapper" :style="{transform: randomRotation(p.id)}">
            <img class="photo" w-full object-contain :src="url(p.thumbnail_edited)" :alt="p.id" />
          </div>
        </div>
        <div flex w-full justify-center absolute position-top-none>
          <img class="pin" src="/thumb%20tack%202%20plain.png"  alt=""/>
        </div>
      </div>
    </div>
  </div>
  <div class="blur" hidden pos-fixed inset-0 backdrop-blur-sm z-5></div>
</template>

<style scoped lang="sass">
@use "../css/colors"
@use "../css/responsive"

.blur
  z-index: 2500

.title
  margin-top: 8rem
  margin-bottom: 6rem

.bold
  font-size: 3em

.img-container
  margin: -0.5rem
  max-width: 50%
  position: relative

.img-container.active
  position: unset

  img.pin
    display: none
  .photo-abs-container
    position: fixed
    z-index: 3000

  .photo-wrapper
    transform: rotate(0deg) !important

  img.photo
    z-index: 3001

img.photo
  clip-path: inset(2.8% 1.8% 2.4%)
  pointer-events: none

div.photo-abs-container
  position: absolute
  inset: 0
  z-index: 1000

div.photo-wrapper
  filter: drop-shadow(0px 1px 2px rgba(0, 0, 0, 0.3))

img.pin
  width: 40px
  height: 40px
  z-index: 2000
</style>