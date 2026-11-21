/* Hindi / English toggle + shared footer for plans and policy pages.
   Text in both languages sits in the page: class="hi" and class="en". The choice is remembered (localStorage 'wo_lang'). */
(function(){
  var KEY='wo_lang';
  function get(){try{var v=localStorage.getItem(KEY);if(v==='hi'||v==='en')return v}catch(e){}
    return (navigator.language||'').toLowerCase().indexOf('hi')===0?'hi':'en'}
  function set(l){document.body.classList.remove('l-hi','l-en');document.body.classList.add('l-'+l);
    document.documentElement.lang=l;try{localStorage.setItem(KEY,l)}catch(e){}
    var b=document.getElementById('langbtn');if(b)b.textContent=l==='hi'?'English':'हिंदी';
    document.dispatchEvent(new CustomEvent('langchange',{detail:l}))}
  window.WOLang={get:function(){return document.body.classList.contains('l-hi')?'hi':'en'},set:set};
  document.addEventListener('DOMContentLoaded',function(){
    set(get());
    var b=document.getElementById('langbtn');if(b)b.onclick=function(){set(WOLang.get()==='hi'?'en':'hi')};
    var f=document.getElementById('foot');
    if(f)f.innerHTML='<nav>'+
      '<a href="/">🏠 <span class="hi">होम</span><span class="en">Home</span></a>'+
      '<a href="/plans"><span class="hi">प्लान</span><span class="en">Plans</span></a>'+
      '<a href="/terms"><span class="hi">नियम व शर्तें</span><span class="en">Terms</span></a>'+
      '<a href="/privacy"><span class="hi">प्राइवेसी</span><span class="en">Privacy</span></a>'+
      '<a href="/refund"><span class="hi">रिफ़ंड व कैंसलेशन</span><span class="en">Refund &amp; Cancellation</span></a>'+
      '<a href="/shipping"><span class="hi">डिलीवरी</span><span class="en">Shipping &amp; Delivery</span></a>'+
      '<a href="/contact"><span class="hi">संपर्क</span><span class="en">Contact</span></a></nav>'+
      '<div>© WisOrbit · Preetam Kumar Deepak · Gaya, Bihar, India · <a href="mailto:pkdnalanda@gmail.com">pkdnalanda@gmail.com</a></div>';
  });
})();
