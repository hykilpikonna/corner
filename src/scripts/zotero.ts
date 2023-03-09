
export interface ZoteroLink
{
  href: string // 'https://...'
  type: string // 'text/html'
  attachmentType?: string // 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
  attachmentSize?: number // bytes?
  title?: string
  length?: string
}

export type ZoteroLinks = {[id: string]: ZoteroLink}

export interface ZoteroCreator
{
  creatorType: string // 'author'
  firstName: string
  lastName: string
}

export interface ZoteroData
{
  key: string
  version: number
  itemType: string // 'document'
  title: string
  creators: ZoteroCreator[]
  abstractNote?: string
  publisher?: string
  date?: string // 'Dec 9 2021'
  language?: string // 'en'
  shortTitle?: string
  url?: string
  accessDate?: string
  archive?: string
  archiveLocation?: string
  libraryCatalog?: string
  rights?: string
  extra?: string
}

export interface ZoteroLibrary
{
  type: string // 'user'
  id: number
  name: string
  links: ZoteroLinks
}

export interface ZoteroMeta
{
  creatorSummary: string // Gui and Burton
  parsedDate: string // 2021-12-09
  numChildren: number
}

export interface ZoteroItem
{
  key: string
  version: number
  library: ZoteroLibrary
  meta: ZoteroMeta
  data: ZoteroData

  attachments: ZoteroAttachment[]
}

export interface ZoteroAttachment
{
  links: ZoteroLinks
  data: {
    key: string
    parentItem: string
    itemType: string // == 'attachment'
    title: string
    contentType: string
    fileName: string
    md5: string
    mtime: number
  }
}
