(function(){
function inject(){
  var btn=document.querySelector('button[type=submit]');
  if(!btn||document.getElementById('google-signin-btn'))return;
  var actions=btn.closest('div');
  if(!actions||!actions.parentElement)return;
  var wrap=document.createElement('div');
  wrap.style.cssText='padding:0 24px 24px';
  wrap.innerHTML=`<a id='google-signin-btn' href='/oauth2/start?rd=%2F' style='display:flex;align-items:center;justify-content:center;gap:10px;width:100%;height:40px;box-sizing:border-box;background:#fff;border:1px solid #dadce0;border-radius:4px;font:500 14px/1 Roboto,-apple-system,sans-serif;color:#3c4043;text-decoration:none'><svg width='18' height='18' viewBox='0 0 18 18'><path fill='#4285F4' d='M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.874 2.684-6.615z'/><path fill='#34A853' d='M9 18c2.43 0 4.467-.806 5.956-2.184l-2.908-2.258c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332C2.438 15.983 5.482 18 9 18z'/><path fill='#FBBC05' d='M3.964 10.707c-.18-.54-.282-1.117-.282-1.707s.102-1.167.282-1.707V4.961H.957C.347 6.175 0 7.55 0 9s.348 2.825.957 4.039l3.007-2.332z'/><path fill='#EA4335' d='M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0 5.482 0 2.438 2.017.957 4.961L3.964 6.293C4.672 4.166 6.656 3.58 9 3.58z'/></svg>Sign in with Google</a>`;
  actions.parentElement.insertBefore(wrap,actions.nextSibling);
}
new MutationObserver(inject).observe(document.body,{childList:true,subtree:true});
inject();
})();
