const production = true

export const hosts = {
    api: 'https://profile-api.hydev.org',
    content: production ? 'https://profile-content.hydev.org' : 'http://localhost:8082'
}

export const $ = (window as any).jQuery

export const fab = [
    "monero", "hooli", "yelp", "cc-visa", "lastfm", "shopware", "creative-commons-nc", "aws", "redhat", "yoast", "cloudflare", "ups", "wpexplorer", "dyalog", "bity", "stackpath", "buysellads", "first-order", "modx", "guilded", "vnv", "square-js", "microsoft", "qq", "orcid", "java", "invision", "creative-commons-pd-alt", "centercode", "glide-g", "drupal", "hire-a-helper", "creative-commons-by", "unity", "whmcs", "rocketchat", "vk", "untappd", "mailchimp", "css3-alt", "square-reddit", "vimeo-v", "contao", "square-font-awesome", "deskpro", "sistrix", "square-instagram", "battle-net", "the-red-yeti", "square-hacker-news", "edge", "napster", "square-snapchat", "google-plus-g", "artstation", "markdown", "sourcetree", "google-plus", "diaspora", "foursquare", "stack-overflow", "github-alt", "phoenix-squadron", "pagelines", "algolia", "red-river", "creative-commons-sa", "safari", "google", "square-font-awesome-stroke", "atlassian", "linkedin-in", "digital-ocean", "nimblr", "chromecast", "evernote", "hacker-news", "creative-commons-sampling", "adversal", "creative-commons", "watchman-monitoring", "fonticons", "weixin", "shirtsinbulk", "codepen", "git-alt", "lyft", "rev", "windows", "wizards-of-the-coast", "square-viadeo", "meetup", "centos", "adn", "cloudsmith", "pied-piper-alt", "square-dribbble", "codiepie", "node", "mix", "steam", "cc-apple-pay", "scribd", "openid", "instalod", "expeditedssl", "sellcast", "square-twitter", "r-project", "delicious", "freebsd", "vuejs", "accusoft", "ioxhost", "fonticons-fi", "app-store", "cc-mastercard", "itunes-note", "golang", "kickstarter", "grav", "weibo", "uncharted", "firstdraft", "square-youtube", "wikipedia-w", "wpressr", "angellist", "galactic-republic", "nfc-directional", "skype", "joget", "fedora", "stripe-s", "meta", "laravel", "hotjar", "bluetooth-b", "sticker-mule", "creative-commons-zero", "hips", "behance", "reddit", "discord", "chrome", "app-store-ios", "cc-discover", "wpbeginner", "confluence", "mdb", "dochub", "accessible-icon", "ebay", "amazon", "unsplash", "yarn", "square-steam", "500px", "square-vimeo", "asymmetrik", "font-awesome", "gratipay", "apple", "hive", "gitkraken", "keybase", "apple-pay", "padlet", "amazon-pay", "square-github", "stumbleupon", "fedex", "phoenix-framework", "shopify", "neos", "hackerrank", "researchgate", "swift", "angular", "speakap", "angrycreative", "y-combinator", "empire", "envira", "square-gitlab", "studiovinari", "pied-piper", "wordpress", "product-hunt", "firefox", "linode", "goodreads", "square-odnoklassniki", "jsfiddle", "sith", "themeisle", "page4", "hashnode", "react", "cc-paypal", "squarespace", "cc-stripe", "creative-commons-share", "bitcoin", "keycdn", "opera", "itch-io", "umbraco", "galactic-senate", "ubuntu", "draft2digital", "stripe", "houzz", "gg", "dhl", "square-pinterest", "xing", "blackberry", "creative-commons-pd", "playstation", "quinscape", "less", "blogger-b", "opencart", "vine", "paypal", "gitlab", "typo3", "reddit-alien", "yahoo", "dailymotion", "affiliatetheme", "pied-piper-pp", "bootstrap", "odnoklassniki", "nfc-symbol", "ethereum", "speaker-deck", "creative-commons-nc-eu", "patreon", "avianex", "ello", "gofore", "bimobject", "facebook-f", "square-google-plus", "mandalorian", "first-order-alt", "osi", "google-wallet", "d-and-d-beyond", "periscope", "fulcrum", "cloudscale", "forumbee", "mizuni", "schlix", "square-xing", "bandcamp", "wpforms", "cloudversify", "usps", "megaport", "magento", "spotify", "optin-monster", "fly", "aviato", "itunes", "cuttlefish", "blogger", "flickr", "viber", "soundcloud", "digg", "tencent-weibo", "symfony", "maxcdn", "etsy", "facebook-messenger", "audible", "think-peaks", "bilibili", "erlang", "cotton-bureau", "dashcube", "42-group", "stack-exchange", "elementor", "square-pied-piper", "creative-commons-nd", "palfed", "superpowers", "resolving", "xbox", "searchengin", "tiktok", "square-facebook", "renren", "linux", "glide", "linkedin", "hubspot", "deploydog", "twitch", "ravelry", "mixer", "square-lastfm", "vimeo", "mendeley", "uniregistry", "figma", "creative-commons-remix", "cc-amazon-pay", "dropbox", "instagram", "cmplid", "facebook", "gripfire", "jedi-order", "uikit", "fort-awesome-alt", "phabricator", "ussunnah", "earlybirds", "trade-federation", "autoprefixer", "whatsapp", "slideshare", "google-play", "viadeo", "line", "google-drive", "servicestack", "simplybuilt", "bitbucket", "imdb", "deezer", "raspberry-pi", "jira", "docker", "screenpal", "bluetooth", "gitter", "d-and-d", "microblog", "cc-diners-club", "gg-circle", "pied-piper-hat", "kickstarter-k", "yandex", "readme", "html5", "sellsy", "sass", "wirsindhandwerk", "buromobelexperte", "salesforce", "octopus-deploy", "medapps", "ns8", "pinterest-p", "apper", "fort-awesome", "waze", "cc-jcb", "snapchat", "fantasy-flight-games", "rust", "wix", "square-behance", "supple", "rebel", "css3", "staylinked", "kaggle", "space-awesome", "deviantart", "cpanel", "goodreads-g", "square-git", "square-tumblr", "trello", "creative-commons-nc-jp", "get-pocket", "perbyte", "grunt", "weebly", "connectdevelop", "leanpub", "black-tie", "themeco", "python", "android", "bots", "free-code-camp", "hornbill", "js", "ideal", "git", "dev", "sketch", "yandex-international", "cc-amex", "uber", "github", "php", "alipay", "youtube", "skyatlas", "firefox-browser", "replyd", "suse", "jenkins", "twitter", "rockrms", "pinterest", "buffer", "npm", "yammer", "btc", "dribbble", "stumbleupon-circle", "internet-explorer", "telegram", "old-republic", "square-whatsapp", "node-js", "edge-legacy", "slack", "medrt", "usb", "tumblr", "vaadin", "quora", "reacteurope", "medium", "amilia", "mixcloud", "flipboard", "viacoin", "critical-role", "sitrox", "discourse", "joomla", "mastodon", "airbnb", "wolf-pack-battalion", "buy-n-large", "gulp", "creative-commons-sampling-plus", "strava", "ember", "canadian-maple-leaf", "teamspeak", "pushed", "wordpress-simple", "nutritionix", "wodu", "google-pay", "intercom", "zhihu", "korvue", "pix", "steam-symbol"
]
