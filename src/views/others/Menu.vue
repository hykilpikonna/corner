<template>
  <div id="Menu">
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
            <div class="item" v-for="item of cat.items" :key="item.name">
              <span class="number">{{ item.id }}. </span>
              <span class="name" :class="{recommend: item.recommend, original: item.original}">
                {{ item.name }}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';

export enum Flavor
{
  light,
  normal,
  salty
}

export interface MenuItem
{
  name: string
  img?: string
  recommend?: boolean
  original?: boolean

  id?: number
}

export interface MenuCategory
{
  cat: string
  subtitle?: string
  items: MenuItem[]

  column?: number
}

export const menu: MenuCategory[] = [
  {
    cat: '🍖 猪肉',
    items: [
      {name: '玉米排骨汤', recommend: true},
      {name: '蒜香炸排骨'},
      {name: '蒜蓉粉丝蒸排骨'},
      {name: '椒盐排骨'},
      {name: '酱香排骨'},
      {name: '四川回锅肉'},
      {name: '红烧蜜汁五花肉（+卤蛋）', recommend: true},
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
      {name: '韭菜炒蛋'},
      {name: '白菜炖粉条'},
      {name: '干锅菜花', recommend: true},
      {name: '红烧土豆'},
      {name: '葱花鸡蛋'},
      {name: '素炒绿叶菜（大白菜/小油菜）'},
    ]
  },
  {
    cat: '🍜 面条',
    items: [
      {name: '味噌叉烧豚骨面'},
      {name: '番茄牛肉面'},
      {name: '黑椒炒意面'},
      {name: '炒面（挂面/乌冬/意面/方便面）'},
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
      {name: '照烧肥牛饭（肥牛片/五花肉）', recommend: true},
      {name: '咖喱饭（牛肉块/肥牛片/五花肉)'},
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
      {name: '白桃奶油鸡尾酒', recommend: true, original: true}
    ]
  },
]

@Options({components: {}})
export default class Menu extends Vue
{
  max_cols = 2
  cols: MenuCategory[][] = new Array(this.max_cols)

  created()
  {
    // Calculate menu layout
    const tmp = Array.from(menu)
    tmp.sort((a, b) => b.items.length - a.items.length)

    // Two columns
    let col_counts = new Array(this.max_cols).fill(0)
    for (const cat of tmp)
    {
      // Get column index with minimal item count
      let col = col_counts.indexOf(Math.min(...col_counts))

      cat.column = col
      col_counts[col] += cat.items.length
    }

    // Separate arrays by column
    for (let i = 0; i < this.max_cols; i++)
      this.cols[i] = menu.filter(it => it.column == i)

    // Assign ID to each item
    let id = 0
    this.cols.forEach(col => col.forEach(cat => cat.items.forEach(it => it.id = id++)))
  }
}
</script>

<style lang="sass" scoped>
@import "src/css/colors"

#Menu
  margin-top: 2em
  text-align: left

.title
  margin-bottom: 2em

  h2
    margin-bottom: 0

  .subtitle
    color: $color-text-light


.columns
  display: flex
  flex-wrap: wrap

  .column
    flex: 50%
    max-width: 50%



</style>
