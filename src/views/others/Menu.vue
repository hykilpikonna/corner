<template>
  <div id="Menu" class="general-page">
    <div class="title">
      <h2>小桂桂的私房菜 菜单</h2>
      <div class="subtitle">在桂桂家里可以吃到这些哦</div>
    </div>
    <div class="columns">
      <div class="column" v-for="(col, colI) in cols" :key="colI">
        <div class="category" v-for="cat of col" :key="cat.cat">
          <div class="cat">{{ cat.cat }}！</div>
          <div class="subtitle">{{ cat.subtitle }}</div>
          <div class="items">
            <div class="item" v-for="item of cat.items" :key="item.name"
                 :class="{recommend: item.recommend, original: item.original}">
              <span class="number">{{ item.id }}. </span>
              <span class="name">{{ item.name }}</span>
              <span class="sub" v-if="item.sub">（{{item.sub}}）</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import {ref} from 'vue'

interface MenuItem {
  name: string
  sub?: string
  img?: string
  recommend?: boolean
  original?: boolean
  id?: number
}

interface MenuCategory {
  cat: string
  subtitle?: string
  items: MenuItem[]
  column?: number
}

const menu: MenuCategory[] = [
  {
    cat: '🍖 猪肉',
    items: [
      {name: '玉米排骨汤', recommend: true},
      {name: '红烧蜜汁五花肉', sub: '+卤蛋', recommend: true},
      {name: '蒜香炸排骨'},
      {name: '椒盐排骨'},
      {name: '酱香排骨'},
      {name: '四川回锅肉'},
      {name: '蒜蓉粉丝蒸排骨'},
    ]
  },
  {
    cat: '🍗 鸡肉',
    items: [
      {name: '土豆炖鸡腿', recommend: true},
      {name: '香烤鸡腿', recommend: true},
      {name: '可乐鸡翅'},
      {name: '照烧鸡翅'},
    ]
  },
  {
    cat: '🥩 牛肉',
    items: [
      {name: '煎牛排'},
      {name: '牛肉粉丝汤'}
    ]
  },
  {
    cat: '🐟 海鲜',
    items: [
      {name: '煎三文鱼皮'}
    ]
  },
  {
    cat: '🥗 菜',
    items: [
      {name: '肉丁炒芹菜', recommend: true},
      {name: '干锅菜花', recommend: true},
      {name: '韭菜炒蛋'},
      {name: '红烧土豆'},
      {name: '葱花鸡蛋'},
      {name: '白菜炖粉条'},
      {name: '素炒绿叶菜', sub: '大白菜/小油菜'},
    ]
  },
  {
    cat: '🍜 面条',
    items: [
      {name: '味噌叉烧豚骨面'},
      {name: '番茄牛肉面'},
      {name: '黑椒炒意面'},
      {name: '炒面', sub: '挂面/乌冬/意面/方便面'},
    ]
  },
  {
    cat: '🍥 其他的',
    items: [
      {name: '鸡蛋火腿吐司', recommend: true},
      {name: '茶叶蛋'},
    ]
  },
  {
    cat: '🍛 主食',
    items: [
      {name: '照烧肥牛饭', sub: '肥牛片/五花肉', recommend: true},
      {name: '咖喱饭', sub: '牛肉块/肥牛片/五花肉'},
      {name: '炒饭'},
    ]
  },
  {
    cat: '🍰 蛋糕',
    subtitle: '（要提前几天预定哦！ qwq',
    items: [
      {name: '提拉米苏', recommend: true},
    ]
  },
  {
    cat: '🍸 饮料',
    items: [
      {name: '白桃奶油鸡尾酒', recommend: true, original: true},
      {name: '火龙果葡萄酒', original: true},
      {name: '水果宾治鸡尾酒'}
    ]
  },
]

const maxCols = 2
const cols = ref<MenuCategory[][]>(Array.from({length: maxCols}, () => []))

const tmp = Array.from(menu)
tmp.sort((a, b) => b.items.length - a.items.length)

const colCounts = new Array(maxCols).fill(0)
for (const cat of tmp) {
  const col = colCounts.indexOf(Math.min(...colCounts))
  cat.column = col
  colCounts[col] += cat.items.length
}

for (let i = 0; i < maxCols; i++) {
  cols.value[i] = menu.filter(it => it.column == i)
}

let id = 0
cols.value.forEach(col => col.forEach(cat => cat.items.forEach(it => it.id = id++)))
</script>

<style lang="sass" scoped>
@use "../../css/colors"
@use "../../css/responsive"

.columns
  display: flex
  justify-content: space-between
  flex-wrap: wrap

  .column
    flex-grow: 0
    max-width: 50%
    min-width: 180px
    white-space: nowrap

.category
  margin-bottom: 1em

  .cat
    font-size: 1.2em
    font-weight: bold

  .subtitle
    font-size: 0.8em
    margin-bottom: 0.5em
    color: colors.$color-text-light

.items
  .sub
    font-size: 0.7em

  .item.recommend
    color: colors.$color-text-special

  .item.original:after
    content: '原创'
    font-size: 0.8em
    color: #ec9139
    background: rgba(255, 200, 131, 0.4)
    border-radius: 5px
    padding: 0 5px
    margin-left: 10px
</style>
