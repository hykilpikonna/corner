import{V as a,O as f,_ as i,c as u,x as m,a as P,e as d,o as _}from"./index.792a4d23.js";import{M as v}from"./ColorPicker.e10ca1b6.js";import"./utils.27a77848.js";var C=Object.defineProperty,O=Object.getOwnPropertyDescriptor,g=(r,o,l,s)=>{for(var e=s>1?void 0:s?O(o,l):o,n=r.length-1,t;n>=0;n--)(t=r[n])&&(e=(s?t(o,l,e):t(e))||e);return s&&e&&C(o,l,e),e};let p=class extends a{constructor(){super(...arguments),this.color="#ffffff"}log(){console.log(this.color)}};p=g([f({components:{MyColorPicker:v}})],p);const j={id:"Projects"};function M(r,o,l,s,e,n){const t=d("MyColorPicker");return _(),u("div",j,[m(t,{color:r.color,"onUpdate:color":o[0]||(o[0]=c=>r.color=c)},null,8,["color"]),P("button",{onClick:o[1]||(o[1]=(...c)=>r.log&&r.log(...c))})])}const y=i(p,[["render",M]]);export{y as default};
