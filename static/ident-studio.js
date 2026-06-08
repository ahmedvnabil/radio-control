// Ident Studio — generate short radio idents using ZzFXM in the browser.
// Renders to AudioBuffer offline → encodes as 16-bit PCM WAV → uploads to AzuraCast.

(function(){
  // 5 preset idents (instruments + patterns + sequence + tempo).
  // Each is short (3–6 seconds), suitable as a station jingle.
  const IDENT_PRESETS = {
    news: {
      label_ar: 'إنترو إخباري',
      label_en: 'News intro',
      song: [
        [[1.5,,440,,.05,.3,,1.5,,,,,,,.1]],
        [[[0,-1,13,,,,11,,,,10,,,,8,,,,,,,,,,,,,,,,,,]]],
        [0], 180
      ]
    },
    chill: {
      label_ar: 'بمبر هادئ',
      label_en: 'Chill bumper',
      song: [
        [[1.2,0,220,.05,.5,.6,1,1.5,,,,,,2,.05,.1,.05,.7]],
        [[[0,-1,17,,,17,,,,20,,,,22,,,,,,]]],
        [0], 90
      ]
    },
    studio: {
      label_ar: 'استديو شغّال',
      label_en: 'Studio on-air',
      song: [
        [[1.8,0,440,,.05,.2,,2,,,,,.04,5,,,.1,,.1]],
        [[[0,-1,13,15,17,20,17,15,13,,,,,,,,,,]]],
        [0], 200
      ]
    },
    levant: {
      label_ar: 'مزاج شرقي',
      label_en: 'Levant mood',
      song: [
        [[1.5,0,330,,.05,.5,2,1.2,,,,,.02,1.5,,,.1,.5,.1]],
        [[[0,-1,13,15,16,18,20,18,16,15,13,,,,,,,,,]]],
        [0], 110
      ]
    },
    energetic: {
      label_ar: 'وصلة حماسية',
      label_en: 'Energetic tag',
      song: [
        [[1.8,0,587,,.05,.15,3,2,,,,,,3,,,.05,,.05]],
        [[[0,-1,13,17,20,24,20,17,13,17,20,24,20,17,,,,,,]]],
        [0], 260
      ]
    },
  };

  function audioBufferToWav(buffer){
    const numCh = buffer.numberOfChannels;
    const sr = buffer.sampleRate;
    const samples = buffer.length;
    const dataLen = samples * numCh * 2;
    const ab = new ArrayBuffer(44 + dataLen);
    const v = new DataView(ab);
    function w(o,s){for(let i=0;i<s.length;i++)v.setUint8(o+i,s.charCodeAt(i));}
    w(0,'RIFF'); v.setUint32(4,36+dataLen,true); w(8,'WAVE'); w(12,'fmt ');
    v.setUint32(16,16,true); v.setUint16(20,1,true); v.setUint16(22,numCh,true);
    v.setUint32(24,sr,true); v.setUint32(28,sr*numCh*2,true);
    v.setUint16(32,numCh*2,true); v.setUint16(34,16,true); w(36,'data');
    v.setUint32(40,dataLen,true);
    const channels = [];
    for(let c=0;c<numCh;c++) channels.push(buffer.getChannelData(c));
    let offset=44;
    for(let i=0;i<samples;i++){
      for(let c=0;c<numCh;c++){
        let s = Math.max(-1, Math.min(1, channels[c][i]));
        v.setInt16(offset, s<0?s*0x8000:s*0x7FFF, true);
        offset+=2;
      }
    }
    return new Blob([ab], {type:'audio/wav'});
  }

  function renderIdent(presetKey){
    const preset = IDENT_PRESETS[presetKey];
    if(!preset) return null;
    const [left, right] = zzfxM(...preset.song);
    const sr = zzfxR || 44100;
    const len = Math.max(left.length, right.length);
    const ctx = new (window.AudioContext||window.webkitAudioContext)();
    const buf = ctx.createBuffer(2, len, sr);
    buf.getChannelData(0).set(left);
    buf.getChannelData(1).set(right);
    return { buffer: buf, ctx };
  }

  // Currently-playing source so we can stop on re-preview
  let _previewSrc = null;

  window.IdentStudio = {
    presets: IDENT_PRESETS,
    listKeys(){ return Object.keys(IDENT_PRESETS); },
    preview(key){
      if(_previewSrc){try{_previewSrc.stop();}catch(e){}}
      const r = renderIdent(key);
      if(!r) return null;
      const src = r.ctx.createBufferSource();
      src.buffer = r.buffer;
      src.connect(r.ctx.destination);
      src.start();
      _previewSrc = src;
      return r.buffer.duration;
    },
    stopPreview(){
      if(_previewSrc){try{_previewSrc.stop();}catch(e){}_previewSrc=null;}
    },
    toWavBlob(key){
      const r = renderIdent(key);
      if(!r) return null;
      return audioBufferToWav(r.buffer);
    },
  };
})();
