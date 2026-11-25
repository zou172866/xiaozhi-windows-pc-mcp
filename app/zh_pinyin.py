"""Zero-dependency offline Mandarin pinyin & KWS ppinyin helper for PC wake-word matching.

Design contract:
- Zero external dependencies (Python stdlib `base64` + `zlib` only).
- Zero import-time side effects: compressed GB2312 + SenseVoice CJK tables (10400 chars,
  416 tone-less syllables + 1261 toned ppinyin initial/final entries) are
  lazily decompressed on first lookup.
"""

from __future__ import annotations

import base64
import threading
from typing import AbstractSet, Dict, FrozenSet, Iterable, List, Optional, Tuple
import zlib

_LOAD_LOCK = threading.Lock()
_CHAR_TO_PINYINS: Dict[str, FrozenSet[str]] | None = None
_CHAR_TO_PPINYIN: Dict[str, Tuple[str, ...]] | None = None

# Compressed UTF-8 tone-less syllable->chars table (GB2312 + SenseVoiceSmall tokens.txt CJK union)
_PINYIN_TABLE_B85 = (
    "c-mCF*RJErk}djPpS1pFx7)o>H%<c==y!QXN~9<+%23{m5@jh;)X&Ute{nNa)qQb+iCSio%w(7`Mnoj*Z-0T^hWZU!73kzRZ%VTz"
    "-OsPT{o}8nF!K{;p?rl}1FQnnvrsQUBL$5bG|SLRLC1v71$sLeXTe*;q6j+&b1s@|te&yILbr|kC;B#(&QyO;&!m2x`h6N7$qQ*("
    "rCFWkJuWBQjJXr?J}u3Ubl1}F%HSZwoD9pq{`P<W`uV)TjG>kRCxqq=MmHGya4bXcgfM{c1FsvJWwb`vi?H9pNeL%qJgup6CD)<p"
    "ma{LmuDrYOsVC)*G;Y!<O7A42svO4>niBbHrN9666P=*C$8Lt)8fT8$B3t#F|N09u0Te^%JfJs){tVm<3@<P$!t@1;1uRyu-NH78"
    "pa+qI#R?i7Y_)M##iW4Ag$jErdQ^4DDbeT5=`**}Jo0&(<3*eI4@s}Zs)*H=?Lba}Tuh0Jzy9fOfBnQh<ab~lz+S;{2(t|Y1Gp67"
    "amBKOl?qk{Xf&~EV)cN-2ZmWnkEryd_MG+w3hMNp()+=w9#;-*7TFrIUEpPr&qIC~Qgp@fW$={YT@G0}RwT$vV5+tMp%?#opP=4>"
    "W)4~ty`Z618|tNM4%SXsFJPmKjTg2$IJVH6;x(b(n|cYYJ9^pnfZGSYE@YIJ(N#TF`SZE_iLX!@LA3(aIt(-L-edWQ<qH}n+Iwu*"
    "arnXTO63w+2WrfyJEdNg&JDU1D5`L2&80n`FMLUJvgBmVi7%zPI5X)Kq`MW*k!e~Ed%fV_^@@<GLZ=7g0G>w(ixBSMeSl<-oit7h"
    "ICF40z}tXkdAd&Mo~EeIg%mpz?wdTy^1j2Txn!17t4OOZyOMhHf2q(MLTL@<1sEZid7UEt4tNvrrr@37Fooj~{0sPZtSqpi(&CEl"
    "5Jw@U>XbQBKA=*8ynwSKHq_6R>jiG-xWD6Rm1hpm)lY-ZA>VIOHpC5O_Ljw2c4s-(C8)}^D9^Pd`CtF^ugYJa1X5MV^uX+B%l>xU"
    "cn<R-99&H8Fw@3NfY}`uDmrS0M;!V%e&S_HxgHrMGTvkd<YsAFr)i%q4rfg+X4u;3sHso5(O~z@gFa7ptg<&4@>RzX#~V&clDUhW"
    "h%=F1=C{Y?=1W*pyHkFDH(+SO+=oRI2Yd9!_)JlyNUc8Y*KGLOo7X^^uD)D(?eo-LYi_mUx-;!N&w%R&!V6Z`XvNxh%4?S!H7#l$"
    "=r$u2{n10aEL`&E`RkvQO`^-s+trpaTiPb}1R71~++pZw%goZSoWW)a+W_`$*x%utgJ&ATJcKiN72$P(xB_tu?-)LL%vCT~LnEV8"
    "pf$vH76(-v6>*%xZ3mB4^qc6n&~KyP!;7g5i%|zZBQmb!tf-Zu)|5ISbzam@QNKl_1x;79YSF4o`z9R<bm-GDPsalJOY$!iWOW{8"
    "7Myu<F44Xy9k|kEBjM_Rt55BhMw^>&oon3=_v+d;vo$Y9ygIXQa}a$k+f>Hohs%#Ahb?~PI2lVSmh?w*J!u%yE=s#B?Xh$Y+I>A&"
    "dROhl<wABTi6doAoxRBi%mk_<u(~iW!zm4yJj7}E9I%|mN*0Yip0AXDl6Ro#g4Q8z3$#z@v8LFfxXnh6?U08(o>%x-;o#2K{WphI"
    "2<<pIs$}LQ<m4sks$|NNsYtOPqk~*3dJj77Zcu*1rUJV*yti0?<FJ8V5!XG8GnC#__rQg&PN~V7XJ;|*8d_WG=OVL$%qlXg%d97}"
    "zHG9x*~!+E-B?bET$*Z$fBg$e3n(dwC}6?(!rm8Qog#I&%2C?(%HH8dE^Ubw6#PTkC3-_<1;<2#S-pe}AKNBQLtKq<d%@!bkG=vJ"
    "jMn&Z@N=eAi)vE}-xSZeQ2&BigH<|bJZUw>i)3fYz9IXme!Rx8o(9hM4e~cA?4WRh(H72@$_5DU5I)d|vF>1Zgxwd83>@clhN%tA"
    "@_+VldQj#<MHPH`TBc}KruCS1LzOp@xr!0Us37ya%spAezy3*?`E~g+fD7f}7vr*EjG@|x>Hu0d=w&t79bC9K;Hl81g+^5y%2=aq"
    "p|e2efQ^pk2ZeenOB6pW>pDv=6`dB(IldP7uH(C_(J=qfP-wVRi>S4wUi^nfyc<mmG>d3{rPYLvO*$^<^rX{=b8RkEG!c~UT)wj9"
    "X_RsTZZ^4<)iA0PqOEgcdQt$&kGc;3=S<2|sVUmKi4#gABlEf}-m*%|>L{Cw9CH85f)b{G{nLLd+gE?yCCIk)n^(tRFTm+S;|1d;"
    "j5{#)G~{m-Ze%glMx%n&7FIjx1lXJ4VS$H@dR1zC^ro9By|c-JX9GzYl3h#TAWmPLr}T0fF#W=>fBxHF&~JdNa!K*)lIC-6PSpSX"
    "$6t^Rpk=^&gXJogZLCdjXp=dh;hyd{dNtV^sB3wa=3v6tDL*$-EUH`o?_XeLz{*1V1cx(b*O)zEzKi)D=KEM`V(o=T1CL#NyeR#l"
    ";*y*-ZHg2G+;6dWm$@z5wwzuPZT0iCSZe&lD@c_wSYedo;)+XWF8A1|vJ-Rj#vMytsl-#a1m`2>7g$uUQ$;sK_l2uB#w|=Hbm?)%"
    ";&PwMPcDaCE%RtEDeu=m|EmI<6{N3Fs6fGlS_^6(L}^S%n2pfO<ITY5o(^rkE!EZk`-(vwr!zd?@ZMB!pnvoCzaYDUo(a<toHKBq"
    "qx(iTp<Te?L2|oa|NQStY9(DLg<zGTebMG0jA0dmKZc+Qw>CVKK=H7AM)QN!2i6v7IcUGJJ-}fJ#|vC!a8<+Y4$l=bmB77fJ~#Jl"
    "-nkm{WW$>s`zt9bCby*dlvW_aoh*xbam7=~7ILjWt0=eI4gJjVmAVTyOWbU6@5ASpzTN5QTNLEHRzI)%&$|ivCKS?8+Cx>@s0-~Q"
    "Y;LeOV6XD?4EG#7$`HBmv9Qp`;!8W*PGPf$L4r3M<0rmfnA|B<rRJ2HGg>Ny*wV|QmqW2fNzBHMogxp*Jl^v-;l+TLU0xM=m*Jzs"
    "$2OmGeCn`@z~m#=Sn6kK1k##FONs4?bZr@DWU`a#T$YAxXWv&)K@=|_)rHgq(jUl;waTxZpm~OF2l_S)Uf^9}62Ll!-4afHI1S<X"
    "gvf(rfT=uYZkQcnOM#w=-8=TDIPc*pj}Mc~Hr4vn%5%o!T*&z+8zx(A9@{*-h!INbAuUyKRAldqa!hd1ds5+A{(!Lr`v%<r9#eSC"
    "AYNliDeWBg(l{96`-q=8WgN=+bPehLrs%|}HsAA7YKk*b&ruYTR;ASrhA|xTaC)f>fOiB*h-F{<w>Cp(j_n1WJxogEb=ZwrN!zy)"
    "E}njX@~_ISP7}N>Om;B2!SM;FE<C&NIb$Wl-Wo?O9qF?bE{nKP-Yj8I#?YmdLAFcnkOn0h88jWx>O|`^X9k?jaB;?yJum#P*GxX$"
    "`L1O2NwQbzw53;)-bLO`NmAc!XzCgLgq@$T3+XfzQ&2BKr=YWGT!V27CRMmqAW@FkU@^dwjpYy8cXVuY>e@BEF%A+Ot#SOu*%?<="
    "TzB<$w`F{+G0BlVC)Xv{r;$gi`FF2|+AbaT>{>j&^Q^{OkNrO1wtU<3eavBBQX|Q>Bs-OSAO%Ni9kE~1XiLMDby~KD_%(^v>K$})"
    "l_2wi@)(Rfm_4x0V7<UrHEj(Vs;pf>D}wd_+B@jXq3gpif@umCA#9)8cR>WP3-1f2m5SeM_Ze+8(`eaf8(4R-)zYpUPw{ZTgRgxW"
    "oH2+n@+tGCLP8yt$S(CJH0aUbMvEdXBU(kYACbSM+lxMVPDPv@a(>K}Et{r>j#`=f8}47&E%NBV^Btede4Fz#&(AS`#*)rUI*@Ef"
    "s%3GfGCRmDk;Pt?7ujrObCN?{u4B37<nGA5BhRx$w!9sA&*c44w)roOjhO>vkC4wpA%LR?{td20_)M^{!gU%qOAOk0H!;5AJEp8l"
    "){fjft*W#>(kI2G4421juGoBWZ^z?+Cqu~}rC60pRvJ33{!5c+{U<8^e3p=F!r%f+AD)Vc3Ybe{`GA!-S~+aYa2VnCh_5TP=G3}U"
    "TWPKfxf>egY3<P=q_as^#dcGQo?JZevBpQ6pC-R5lHW^VAf=YLo*WIeu3Gk0{rTJ>vxb~1;g?W+L8%I*AyhU{TSK=A^Be>zi29hi"
    "Vc~(cjl&y`Jsdx9(Z|IR*B#uP@t(m)9iud5w$vKXxI>c^O?EV|(L!mc6y1&#nOupuu0(i?2UVW;d9~!#lXp*ko;W=5tEj=>m`gJ!"
    "?WOeQ;#%Um5)G6`^nU;L7dl7WM|kLd|5H(Z%ZAnfx_h){D%!ZP<!AW)oi3$=pV))U1=?L0bm8H`V~E)+))Jh*@zkegjW!=Hxm=oX"
    "X~U&Z-7M*x6j$O{zn`K4AXSEvQh7IU&k#=GRl$_<%N6Uk_I|7i@f4L?)IL&oLfZyyJKE_A;uT(WeEk@;PdkKB22>%G?P2wfvlGs1"
    "xH3t}_v}r~uGIR{oJ%jz*DJDa7O}g)fsOMIE_@73d|c?-q?gZ`9p`3Ti@9;-;hVjw6pCVw<W!J*S;D%0f-VEH$})Pn(goW)e6%$n"
    "pNe$9{=wvGmYd_R^!xC$$<MQL04G&W?vme0!4-2RR#x6yNh-hoSvNdhkPo5s0_zF&6VwB^`HGXZ>d2R{w#Cs|aWd|etefMdkC8!*"
    "2eofBOVh%mU`m%NJsr*1=9<lb+b`}9d6ehPi2W*G27I&mUgu=Q$w6GDF#Es$`M<s(>;6PFNbkWcfHl>gNE(ogFm=Y!2-k|zdQ=#Y"
    "eIzHX96%3?o@aW#IX&U@meVIT(#j>=T*!8+p84N@L9q@C3#}@4EL@y1tSIAf-qSYeZ23Op`<mkmCn^whem1p1s}3X?EcJ19z+;BY"
    "p*CpH`P%V&!*j)9kHf3vl~)WgeR-wT{aRsWo9Mi->EO9Qr8R8_v{f1_NB)FP4u?u?eWb2<ccZS?#vbUFKq?QJy<S$uxe{Af2d+){"
    "oG_!pZ-w&;F3;#+@ao`Wk8w;+in?hE3<^T}6zHSKy}*SV+gYBRq_0v#2bXQYbO(nn_)qvKO@6~+9dCW=U)4S&qi8_|5Y&dh{fp*K"
    "J6CBgwWP|wZW9M7Dvqgsq0U@MFdFY^W@yjmMjV#)TQ|$n>`9pX`WL0v8xC|F=y@>mVPe8Wp~?!D0j6B7xXKN*RZ(=nph1~26>rpj"
    "Qg=i9EghPidT_SD)jU^KMLglhos)z=ef}II-IQ8chOvz5GR~@%bU$MTnVIf1)tk`m!>9z~gI;Yrg5V6#B|H_d-XY0j%EQtXXEQv6"
    "cr2jrQ0+p^9!0j6lT(8?4hK)ZJ~%G(XDz9tq<l$_q<@!TK~`ParX+IJvKnc^pJ)7)n*~TUAZI|KtmS643$3<RoU<atA;hZwEop(G"
    "-aLEMFJE|JA;F4?MjmTVY&5a6#I23TKAw*Fm?*u1?}JjGn1s}>eod3t!exA+<Caq@z_Z#wg@_Apwk>uI9@Kf@{L!GBkhd-NM|@La"
    ";3>I4?^j`3z7NG1dJ2@zT0<UN@NS?v!G?j88E%_+3aQv5>qM<Pb*toV$xG2;z|{#`TW)Q5{NP!E7apIyue5NrocI^re^5d$<!Bx&"
    "rJ$67(g~~%SZipd!H?logTRH$9qtC)O~rEXDZ{6ZnOM_Zb%s?R?T+TU))MCvO?Rg*UcaI;*Cn?@?tm5#TApbg&^n@*%V9~<4e2S$"
    "xXWen>tB?<RL&oq;MIbp1IY!F2de767g0Ya&7t(h>Y{&quSz=VU;po4Sl0Jx(-bezYr=hqrG?gEyOI`RX`hNEeg9}nvnw6SbXsz_"
    "mrO%)E3tdh+Wq<$6@TyA&u0$C3mOKr0xg`nDZLRTnY=lyXRw>VbEx+fHz6Kl<)&p)%f!|VI~U!kQ8Gyd<vNAC44!Pf&oMGFQY3RF"
    "Bd4}c!y<VLng%pWXz$W~Pv<LLbM){iZc;L&&sZy^N|mb-*9PolbU-VawEXbO<kgUOOZE*uxnH!(@n<V3TT+gs8am2Vh^IG_d1}>U"
    ")ulDm5kE0ydzal*_Brt{>Qz-*cLM!S-MY+v%d96fpD^6PXb0mVOe?T3Vd23e503`CI+$Iep=ct5^BJy+xa#5Yqid{t4Ie3tikO@!"
    "6H@I#%^NkJ)EiK5N4<ciT{?|<(BS7$N*{4lS@IS)lA$XTSEimsBfS$9dU5U>eFG>O-{?z&6@y*Tp|@Ya+0<5l@6fDct*qngT%u`N"
    "E2nLZP7S);m2%QH8*F%+;q8Y{Sq?IM@A2cJI8ur;sr1EZNMj_8owzMImc@4^c&NCO+ew}(u>PS+^LHOC@2j2-XdB8@Fq(rmLMuRb"
    "f{QI)3V1K$b4R6+$__O~)Vff+LenvA9ZnfsyzubO^E>Ybe5&%p<mZ&b6i2bfKKmx8k_2zH+Ls>FKT#H}Bed??<O)4%V?3vH*w$*)"
    "d{MipD~M^4Q#VdeIp62{o!6>5RRuytid`{1amvzA+Up>mszvXzm`hw!&)4KP{&`NNp)%w*84rF`R%NSD*h3+LVihWDFk0X!z%?Ow"
    "!^Oe$4vQHap3zf?V&iO#rwLw1)ZEj=rh~)THD|AET(~~t){b|k7RASo+>3JG$#W-hOD(5(IzNDNUQ<?cgq0Rn+Su;ksDaZhb-J`&"
    "(soa~BKa}rl%~0G^U3`!A5-i{d@e|;BdJ8PZ^?JXIY_4~y@~AAtEfkJ$GVT<-C)vzr31^ou0~%=Y^QNJ!1)+Awju_+5Aa#T=Y+~t"
    "DtD<;p~{quJyq8<oY63(=|t~f_o984{JBP?OrI+jdn@)rUVfV!+rytdT@HLR^@gfdX{Dr7m2OeIhD<)Pt4p*|J5x!8?E|(SIKSfY"
    "M)^FA542PAtxb;+#Wk%{tFAO7b+Ll6d<F6*<m=!SV6g>11V4dO6;m-5hC0rb(402-^A_Wvm7>y`t)+Cw(yyoo=pkYS>^1bB;Lc&X"
    "!E6zotG@5I@_bJy)23pX+>qRaye`f2eD3r8spUdCEoom$RdI&;soytIPgMd!ZK#~qb)}R0rh*C&7sfa6S};Arybp61<}bZtW%KMw"
    ">*88b3;Dqf2X`E1aGcgkc{9}oV=||>%B366PW*U%MXOS*>L*aNCi#heG5aZvn)FiAH)M0vxS(?JHTp@)Kc5_=-``PbPFJ|~tR9#)"
    "mU_CYYlso_HsBs%v;nW8N4BdwtQWBUz$OLf9$eOXkea?@W{cSg=2}>e(GJm7KC+aCAhSd5D|Nm-*~xdPnm@2n;o6q%m|J~z=j<lD"
    "c6m4FW5}l_M=rmWjIktBmt00lTd6jsS(N5O+Oc#kJ?`y(^u%{kl}SyeeVGnr-jKyd_JN#ix#Z+Fl-o$|eF^gtc77>V!~K&LhWPhi"
    "n%B}Btf$#|%fkMp`AtTPU;q05D65!1?<?d!khdZ4z^MTz#Yi?@ExexbGoeyK)`C+er!HKqb8zA7i9d=NiqddoZpz{&K}(*`U;p;E"
    "zkWjNC$_<=!)T)0+|B~eHN3R&q6%W0>=*T3x^3+ZX}_U^MejbnPaNE(y^wB7c3asU)r0h?;P?|8P@F(<3RYD&(w_wgCs=Oj;Yr8E"
    "=?xbRTvzavd>3(blRUem{Dz86s`%u1)GFyd`8c8Zp7uBLb9y`)rYY>vC(^Z^nbmFZ&W5{6%6RMzcsk`^!`BUmK7X2G7sYX8=F8$B"
    "kAvQtZt8v@H3PE`!wM|d@P1(FgUtlnA$H4px-c^Ig!J{|%$7?#u3UJnu;?Qh6`n6~cG3;RJ<Dz;hpPk`xf;LzO$(873(PIp9<(N~"
    "%))C73eThIcPe?ta#;`6+BOa>J(V1XxLjfI#K({fhdO8KmB`!BqCtM29tK4|eJ0<jn2N#%SCa2&JJ5s4*_F2;`|tmmd#QpWCj(1{"
    "Bbm7}8_BvZyQ}yU3C?P3zehLe1WJYP1~<^V!f*`J3oHy+G+}Xs&j)i=%+0Yd*Mn;1q>+iU2xl>#Ts$YJ3X#-}a((Td+LYXwMhyze"
    "6m01-qN_`<2UqsopYTMFIVIm0_oS!Phnt*-62uZu^<Fgfob=$c;6rh)XP=ATBQ_r{3O!cZ4Wpe7j$nqtTE|}YO>LjL26+QgWE&5;"
    "Ue^=QL56n^KEC-};j<DyIY}udEQsMq(~#*}W;IzJ<#LhR{@1@LWf56FZxwox`j79(1MDTmcdpES{oC&mNR<DHx<9e?ou_txKI%^&"
    "GI=Php}2#xuP5N94Rr$?TTe&2Pdx;mS7E+`Wf3+@IP~FggJT1H6CNLUX1;UQ5xfQvci?@2_Xm;<rcyd8%6F{vbyVnqN(E~bbavQ!"
    "V0(p~@^|c7!%>KnE6&$?vUD2jfVrwN?}+!U9xg=|mG0EsQLFqXc$OBev$S8*u}4prq711DK30j^<;<LOKId)D*Et_@q0i-%9xGLP"
    "$^<&VjEGI2YXz=t*lKcr&4V%zJsuu;I^^k@=S7}3d0F6fmX9+Iyl(_9`8wvC!M7gYu6$Rp-r=~(al}cU6PrI>$-0soNKySvq*Rpd"
    "K)S;}(;#oB=dYWmj@V;Pj*c7^EIJYxaw*7VC9jRVo)Se8#lQZp&x^dF{s5;9-5T`nFb`p_h_VHz0i5<&a<Oyd?1dwP-z#wn>K>)("
    "pY~5U1?K{z27C-G&9NDx_rzreR~tOP@m0pSPHtPD*}3_g4Y;!4hN>U~_7{B1i_?={Q?^g_JY6hqf5PPFl?Epb!woE+aJs_j@jb2+"
    "Vs4D(5mu|%NMUP=of8fVIIiR5gPR<lJ9>0;Y2wYr#}bn=)mqf-Qu{)EpT<vG6=+?e-G{C(ic*{zvhm>CjBj_ow<K@Nsi}9NQt37Q"
    "iOWzZLE#9cK9pSuIuK0Y+QCu*OGPZZSn1%<!`TE^4Sf3e8qzqXLqzv8y#k7#^j0vV)Xq_g2QgxCrqbPuXQ?F=|Jxs^SHP*EJ;Gst"
    "t2D0E#y8YAsBhCSq4kIS9$i}WRH9}~Ns|i}J0I?(dAnf$B-w$~_R=g!%a&bNcD-MJ|JSbpBjYEUK+1r05y}BrKG-?1OW^p>SU}?g"
    "ogs8i(9LT%54JE->OaI%2~892G}<3*C}|qwDvt+K@5v0P-P1l)3^g(7SftlNheDym#S)jEY*}1)wO4zwrt)bljlHzW((X!oChfU&"
    ")6#Fsa3fPijiGE~*=00MUv0U5<oQ-x|Hof+TT-OoLJz3AKd&A%Ec8O^w77g^)8)rl|MKsDeyf}WjW>)l@aki_fbJEi9coo*vZu(S"
    "B;-_!Qy;!HBr_H7DN|Jxc)$MsmFbBK`90*%P*DnO2FD#7kMLN)V*^nFaRbu_G|t$Jw2!?ro=0T$X!fG{lCF7rO*wPsUda6!yBl6D"
    "IGFNv!mqt{cHNbcC5N#jeYKo|!1oZ01ga^p7BHH^d<=6BR&&@U5X|76LZgdz0Y^C;J#l4VFvd$(N0iZ_{h9nB#dS8uy!3fx@n+Bd"
    "m@hG3l|brBsV3#QI2#!Szy7Wki%*a}gEiOjRX>B%f`$oQ3)~lYCamgkn8Wc7*CV#_IP!74!l{MJHZBwNGkD%$@J3aoM0G0rRQ^!I"
    "qfYz{%m<C$G%L_1MaL?=RaCY)GvRENizXXOwiJ_|*v+w9VeiZ<hgW^xq}k8#>A{y0UlV?3_<iPNAo;nJ9%3HFxk_^?Zc)!6CM%gF"
    "vR%veB*9g#Gr4(kkL0<PFehPG;)Z%jRbZEnP`07E{J!EHS{v{ux_@o#(ad1YMHQ=!0ljyecJu~ICQm;+&#>Q?iYkn^@(lHp^nt}T"
    "%=ehuqG4n0ileK#OaJQIC5MzM9xhN&g=1GkP}ax%3~MpACpa45?1lRZ-go%e;yX`ilWd<FUFr_WUC?+yi<piRx<Bc4r?<_eF4snE"
    "<=M&c^1`bWuQD9$`07jjChe(o1~OmEvLL60Tr7FI5?;QW(T%CHj#YhZQR$x<rWFoeagxLJ7OziyPpR_Y>{(K-x<S3yE2UL^MwR++"
    "D1E?Of_Vh<42>cT4lo+RatG@y9CPr<!1Dwz1EK=F9dz>8JYnyF;{i@GILYH=^+oguw*%Z|@u1i;rc9nPO|rIBKan$}-jn(zn&fFw"
    "qLV@4RvUb2@hZowHg68>PdK>p^~R4ahj)%ley=6-miAgYFX?w=f0unM$AbiQxeVl*5fvthN_t&}J=aiu=yQ>e2R;*w`WPkJ*s~r*"
    "0Z$Z_jyS1^pVF2KR_Z2&vLy%VJsls-J+ypqJK(B6-!MqQz<_Zdydg|$a2>%jhOqN>zbbAO7_adf;eCS7C7BJn-*p(}yIg3o)!^Qg"
    "2S*+xJS_3>$zGOcA%BXJu1WSFRYRJ2S!QL~{q^rERPBa7DcdMfb<e#-1LSZijqR`R@cy;X?CD&gV?$>RhYcJrcv|9ZkGF?L-ruCD"
    "xIUAU!c<lV*%mcj>N!3X-+JzC?=)SuE8shDGj)_^s+ubcYu!No&6rLbomeww=Zw9MX3TT>|Hqj3n<mY8s(2HV8KwG^?ozHoxrROs"
    "t7uda=(|Oq6W7LUO}MW-=t!m`8DH`<>GoyZknu*mhfEaKHf0q_oc#LE>cLj=cF_m;l=FrMoTM=*P^LwOM@CH6hHf>AI{M&jY0DE+"
    "bFpX1H2dp2R<|C|@U#m%3+NZ1UxQf-7H?RNuv@|23jLj;DSTKMmGI+H+WMkZgZd-Qq$`WgMNW0N;AsMF=eal5vz7CfgR*3Ynno3e"
    "PN%>AU3aHiP)Rgh{&rhE2FFy+)1%(D3vET6b?EG&UxZm*`%00fb<m~mxTuAX;?3ZK*Eimu_`H%a)jo8~<W}{GVwD}sgn|Qw7sa9U"
    "IkMq$^`Qy0^WpxDhc%wtyuP!aRh#2Wimxxe?f9LOY*xA%>9%E1mC^dkq)*vzz5qFq`&GiWgmX#sYpAM81(T|eqq;=SukV?Fx3Z!I"
    "NeR<ytfjQ+zr_0M_oV){{$3D+7sKkP`84flKPhb4?39PNn^%tE$%NNMxfOo>yLM|M{`GqftccWpLn8-A3*s(XB|LBO(!gsUuUn;#"
    "DBGo7T{GvktH_gHNBU&Bdf-;X?X@P-b&=O5A6FdB_&w(LNeo-sHR)Vrzt&uORV1L^r!0~A>pK-rd`R`6)(5)=cC6QLmtoWb&x6FI"
    "N=)XJx)mC}$T#UyRJ19nylh_*)a0H1r6;^$?dRn{;S7cAA4jVoYkt7p!R!vR2h3g&+#qg4yn@dgQ$5V~b&I29Vadl*fTcT@RH6IC"
    "(i_Vqtjw|P>M&Rga24ojtfo^1&mX)w7`8FmWAwx*#LtGZDav2Su*sZ~y`XwuGwotP%HDg6E){wz74z2Qm7Q=w)e}puTs6(scigLT"
    "ug1eGuT&L%=7Xx&p8S4D&Xa<zc~#Nrd@uW|_~$<qs`;~}DKtF)(&OTvq^$9=l7)&5W*e$C-TP5ksraGMf$j#zZP@4G@Pw;@<wSSl"
    "M;=a=ICpXV()z$msJf+QL@iY*-N-A`<f`?-dPJ9$=HAMgEuS}PgGvObj7y7olg3K6c?I*oeov`???J8G9_#7;(1M{4vkJ^ckW_R<"
    "SG2MCz?F@UBR_|~zVmBn&Y|7c895lh=n0DqENs}ja9o0af-u6&469SLT(qv(uyNAB{S*%kJWTK!;%$ca89sX$-xb<na@EwGYf*kd"
    ")r4w3)jQ;zsI#T6f{`{2HZ&~IV?%L+;tS`xY}ffb)wrBGXlTwB#lA~3k<CW-WATd;Y`*M${Pky4UOYj24xtIjT2puSj(HP%S?v7}"
    "frn#^%9_UWCOHG@rKn$|{!pt<KTW3<XBu2_*xYf|;bDiz3*J}wRFiaF`c*lV<x-R=D^c#(zkjEseK38fmNmOHTF~x+JNnaKRJK)A"
    "A4Tw1#dA;D8Im`qm5iQZ`i7Y%W<AWWbby-+w2s(oV{eIlRXk_47TAom5b#szr}13GYYuN!ZO>Ph(kIFeD7zsm&<H#@&~QqtH*KbL"
    "yD3pXQI}$aK7BT?TphDxbF0Vgl0N^@d$HT(u@Vji-mZCf=HrF^9{T}ba(w-8JmjRzpR1%>(!a>qke6Z@J+<BRv0BhO?G~_W;dFx2"
    "vla_8#clOJk|9(ZeCT~WCS09!{lU8_`)lb|Wto#h=GXUVK-~M24|Y%EzruvRuc3avz-%2$ZQaIcmbE&FA3BTEJAG`p8tCLz39xw5"
    "vZ_*;Q=S$JwTA|FudPC#gEe1Qe6MJkP_Kxiinpz__R^0Or2qOqx|%kB!p+ZX{1Y2LaqZ`Qhg2WZCCJ~Qkb<HI#R$q7D372#he{oc"
    "nLbonz36eYRRQY_b`k8dK7imHpfP~v3c4p4BrvMN_yyBge{HjDz-kApJ?zSGn1P=GKL_VLoNWk9xE0}cg~u7f1l~5h>+mr#wZP01"
    "vo$P@u{6a>3e7pzYG^4{k;O&@8y<G1*k9vdjDr`B(m2ZF%)nU}7du?sa5KiEi{}8ZX}s3(R>NlrV@1~~{EVrR(>>x^otzo9p49s2"
    "lM(HNx*m0J)Z6MK@548ZJ~Y15%%{bHmTOx1w0hF|L2DIXRodrhU!r4+&eiXEiioZ$y7uX2>&o)pqsNe*3580V?kJ4uwW61bN2MQE"
    "6sgQEQ<A2nNJ)t^Cg&=gYjS1Hl@l91u6k^z*?Dne%&ix<EBb&&r_4R211CI=c#+aGWj#l%@Vdn7E*~sDPWZUs<B9z)pHwMY<wu59"
    "_&p+iy!qMY=aNI4!!gG`Cw2aWlFdsmudC(0D{evDkqpu@c<bVM9LQ`ci@GcpvaZQyA=`!QBJtmHy308wL0^KM+%37k<T;U7M&1>9"
    "E4s7gT~`sNKtFo?#P6T?MK=dBQ^<x;@&2dpQ`TkT-1^h{*`;-v80;}U)ZL%b2rDrfM>I=lj?qle+F*N#{UVO;I8yqjhfC$yEZ#!0"
    "O4JOflc8>l)-mlbbm`J<z*(2g99R2Xn{xfY?paA;o|bs}Kh@#G;?GjDmKc^ei8MOWYKS)xZ!g|SrWIMWWObH}su9ong>(=0{_{CN"
    "Duna`vNOmYAXkQb422IAEhtx^T!-=u%3CO_a=!`{#WziGo?0-wLl~+eAP<uerlD>(t(N-W>!}UV3*LDw8TyEGX^)1YlL=ZWw6bX3"
    "(00-JU_-t53dei&b~sDne1MA%t`s}&a683qgohZ90s3zYj(E%CeT9z~K5cxu7&h^h*9T~m4t{p{c~QPb<s((9->=W=<j!bVr%_Ck"
    "2TjkkZ_)lmhZP-F@omuYK*uYcCiLi1IHd4EuQtUwif8m$a%RK%GnY<WUUFIedDiU{bH!ErTYHAwUh-_gOM_Qw-tBl_<U^ZJ7N2%}"
    "sqtmb*E8Q^e%$zJ^Yg`FQLFu8Eaj`1Q>j*@dJ}sk^}5s@X<E`eO52fkLpoRKZ)LQXkuUR+EPOdN<rGRVlTiIUB@9(metoA8SKq+&"
    "V9|!f8Www44d7IOQxW0^#L58`oUCzm!OaJc2Rx6ppu80@{!nF1)f%-6G;h#lt(E2NLXk`HiBlUkFYFlHoN>FZ#pT}RW5DO4<j+!="
    "OJOg)s$9x?6Z&I?`OiE4NmTUIA*=q}|2fB7jlkMzP<8gu-b1en{TP;QSj}KvguSVi(Pa)vjD;$iQ?&EwgxK6-tB0)}dJ~*GxH0fn"
    "#BhgESAWs>Nl|V|MT1I1GA7ioQGZF}9gTgOr)k@!LxnC0-A3OE!KP?L@s+c8&h<He(G^Vj&E|ovH`^O-mAO^pj>qnp#{(W8c=_U0"
    "SvOuDbB?q8uJQZBNmjBI$={@K6uTyklgzWSc*&+E+ngMC61Z}k%VR0wR@p?41sC5g;sa7+NKG|XC>Bb$AXC?fQHr6mgGvn5jb@m3"
    "2|8m<GTsXob$#}AWy7Wghq?xka{<m32)+l@OAeN{`Z$k*5hM9?7`_(h0;+M*>@l3;@Pv~TPMSCw;^Lv(4%?9?jpr4^A-<ccil&-J"
    "EtA@9YA>i?py5t;CZ{u6v^62@4&<ja325LSP}HEr=SoI5o{cM8YYp4|6;C=mZD{0K&3JR-t%B;M#%X^i-MjQ_8g`~-g)<sr14kJ~"
    "gH8S?s{AAe$mSu}g-QTM8mcd_$IzRDr;5T3EIY89gWrbJ8y*Qnb+o70o@1|!Jp)%Cye{ze)TftTRm$$E7?WM2dP43*cZ`;4Iv8|a"
    "(L+@LWr};8F}S*6YsZrhFUGtKcxCfG<dYH<Hs1>TQOw~=Dv)AbiXSnT;^d{dl;K4dWm#(I{|<=apI7VWwf}jYAXk9=RG-N%mY{U_"
    "^CHtMY*r9&A<02f$7}@)du;8oW9p;WJs*2_>_@ou@MPg7!CMX=Yf6o%S|w*poicTLG<0e6rrn5kW7^&6Xj5>ZK*h94_Xb54XJXFo"
    "IOlS{#Ac1F1+G@O)@CcsgNP4H{#+#0(kHX6iL?gNJ<HIMVMB&(8TMq@m*GIXzIfL^UENJzl#C0W5^GNQ7Nms?6fAI8FnoZ5=yLYk"
    "tx|R9nYN0F^88uIw5G1sRbc)n971*pwGB8mXeBz;PiK9+*iy+{$Bx4I4UQ9>v~aDETPeOL)l~HLX>+H`OJ{6YpqIzRm`iysRd_h("
    "+vR)K(UERW#s?W6WpBy;D3^^q0(p88?tcBB-=`;^i9X=`9{wbcpU)XmYe?N8|A6ueDn&4kx^Jwf1u30b>pS=!oKs)PR)X^qf;BwW"
    "m|bBp#)?WVNB5HrPdBj59#)T7+v}3N(Z|M0cbIisuZi;#&TU+laoNR97x#HhQkN{Msxz~vT!$(K89lQ5)c&o!HspD<8q;b|+XHRy"
    "bXd@-%IQ9r%52PZa@8ERDr|-9jJdnghnd|14_uvSvxFC_O1<$t$M*|AhWv>6IpXL0_s0v8eoM}gTquRxpQFykQ=EY`o6_{ft?ARw"
    "BTq&<ncijAkd>nWY!k{pl4DbDJ$a8M$ts)b`Y^SG$_=Uys4HkH!FsHloR0{rHdcdgL0G}r9xppoL1{dwnbS4i;6cMIDFG8rDamlL"
    "$gK(w9_$7DobYQWxt!FV(%eYPl>S>>LvD2m^`Z(j!ycrXkZD2T0;MKYGEiG+XzX>Nch=-FF2H;bhlcjs)x}B~Yu#_Rb*H~@m^^eN"
    "er4cB3FQ}F_V~8`P(|rRMuw_NA}`6ZsD7ofDl)EgYtijaVV$DkABJd+xV2`t$6n0K{{P1ZtM<25Fyljw{S==9-GYs?(wa&~Nvf>w"
    "wqEsn{66uLOGCj0Go>wPyD%)mMcFQkojp!e-uLmdCnF;3MJ=EDs?>W@Kh(C{&FJ9Kam=+ITc&R7L{-U7q%;zvAkONK)m9(bTJkE&"
    "tM#YJ`yWc2zE@D4fYa1!)bXL6g3*by8=hs=um7P+XdjAaD5vz_E>KByGp@bUbF5JSK?dR!d=gB*F!#V*g88*psJa*P(B$#zvQy%7"
    "Nop%;c+#v%^D1paIv;V%>hbym)hrbHP`p8@0!CNs!(mN-L~}E-G(m5qrQJ5t!fasB=tN$Xrdyg#xOU=+V%m=Gg3c$BsCRPpBmZ}j"
    "IIQa<0j~$<AOABtJp5^adW!HpRnd#HmCjv{2)DkRi|Y3O`U_4G+!SFx^pvpqKu-?}DLbc%!>Oz^Y#DmL{^MW&g1iAGRT>)l43Fi1"
    ">(+}lU#G4CaCU*?6i#^vrf?}@dW&NpXE$8NxNhSy#?uy`#otDva+<8npW`}il}08_Mw(k^1)8;JRoBg(tw(W2w=Q+Z(&TNxe!@}2"
    "??6fyaqiL_$)q4tOQs#!I<iYEo2ZTIZqIj{r@Z<vm8GpLj64`EVX7Q=!D5jNk25~c%Ayb<De0?zw~v(tKQ>L$qzs*N#p+1oqm1?I"
    "KfZ9ETEKV<hbCMLn6|OD!nvu(v6ByNLfX9OIN-d(h*(|!dmdL<Lh14y$<4spz^(&+QBUH^Gpu-M*3d8L0o<TUhb~v<+>E5S{PiFI"
    "{g=MYnnU9N?F0rh@KW$<z-y=PQQ17OaO7)~ZO0Tfb+>MM<=LF)0YBD~f5`TsyO00zrvc~OV047}Q{By3UpLFPMSbD!d&#$n^OJ1!"
    "5GPlHMh^x{7<OP%0aY>#vbqa%tCHdCHr(OCxhfYDc6`a&I;9V3IeYp!%F;&@j&JZ?@b|hqqP)LP@Q|WyqCK@UzL)7vZVn1clTY-w"
    "S`8ZpsuYa%u&MJv*TYd6Ck4EuF&t|^8r*68A@B2NFnX}*@S>YRw>G^tysfj}VE@9`yQEyn3?=uH@>!2ZZz|uvBPvV3+V8Pcdkk&0"
    "S`!vE2tJq@Vs44K6<P)Cn|kE*%HsP(Zu8p-DA0C6+ZXwDI?X74aeBk)8y6F<-`G)g`HF`pje3v0{**O!(*q_?$zDU2C0TZ~agGPI"
    "3uT+?4crq9m43^@><Om{Ty414F_YCzrEWp@k{%Dr4ai=zRpYrS1yh=1>1M^N%Wm==IR$c`tEg3vREZlku)INIqUVu~t9F$){5Fj4"
    "R6Nk3#Hy}OWwb#$73nsA{pa8Qg0cy%CA5n$%A!%m`U^WgzElluQcrR3p7RwwZ!dP)esg2UovLm<3=JLz?2S2@Nyd=+NIgq`w^o2!"
    "0L=)lSx8!tcv!sSaDlTs-beVUQ)*AWF7-zG*+ZL#BRZ$)tZ@6xc7-o}zFhfM=385ewlre#Vu@Dj*~(t|6ST^(*+S5Vn~g;Wi%tD_"
    "V~N%qn@w!)vE9IS3q1>$IozwCHbx7KXBf{h8K{DnQVC@<lrK}ULnW8|Bb~E!p3!SSae?0FKX2GQ@chb)IWMlfT=DYFtCFr*PbW#u"
    "^oNIqjI>tLF{NAAyHg?DdcfF(sDgtIE}MAU>KT)rr@Bw;j6N4oTeH*QQHGyo$<`!0lBQB=1$DbVp}&W0A1W89T)`NDlR(ph)dF@~"
    "xMr|ALZ^g|;*|^DRXMVxY>&!Qs(RE}Q?Ed`2}J`=r8zbIx+!MYm;6==TWQp#5z6uW>p!(tuzy~KpC|){16Bmxk^U%q@y2xqFSb(r"
    "`k2I}p)OUJnb|>ZqA%=@VC29g4YMw6(y;4dLzPE$ylnB_!$%)upWGGADx~1=9MRRMXF#tasX`-O(Ys2?lT(f^C|U_`bL=-IA4=;W"
    "?n5Rja=w^c*8kKR`T<@ICOJ?=cW$OXz+8-IJ>a(e>pyj~@~yW2^RE6R#?Qz8`SgE2Tga3k_kdCdN*k!SP&tEfgPMxPvi`hSMWlLH"
    "+k$!<8me|w9Hn&87={B_o3QhA(q~qf&tWOi_NZ>K@y7lV-2x7tIIQ7lfu72x8hRa^u5r7?-57UUJY4bcz+i!wnI4_q+u9nh6~+xr"
    "R+Ji2x=xieRWf8e$y5|KBD+9#mFh=wvefpdy{FFfn-VLPYvjGiOY~`%`IhE8n#Z&}>Mt*M2JJoa7v!Jmyr;mSU`Ur5T>`p~DW1?f"
    ")MLHegN>5z8=76NHo2D9Ut~5s?hM$i@jw*_E%qkt&3I<9QV^SxK5h3H@q5be8NWkGEhW2@!c~f<&cM2oEE)Z^LH{BBr#>p-USyES"
    "Fe6i6f3mr)=mRx=S$s>*D+wkN%;hqbFe9&yL^b{SrcPkp1pI6uRnY&cE800^3$RG*&x4~frUsZcuwY{;^{2_zoMFqwb_S;(Tnu$G"
    "9~*c);(e_1`EB4QL#co=u})^&qS1{`2|cd#d{LCu5tnXqdd+E{b5AZTxtQjnDi|7E3%GIQX2@QJXH%ZdI9A2qNGG<r*FC11DOO4B"
    "qcjp}oy5J%sUpGjyK`O7PJW*ApV#sC`#^pJ#WJ+lFzxHowU^*DjdKInb-c$E94R={WyNJ*1Hdfcox`WR!T=7{!Y0QqCj}{5-%7JD"
    "hq4@MdL`W<D*S;31qj^``~~<&IE~=6fT)d5Mgu~-r*l`8hYAylH4O;u4)roLI_TOk>{EQ;bVUQgc+BCOleeT}v23wzaZG8=WLJ}i"
    "FX2Wls{aB50}5}QNtGEmD;PU4ZlLMtoKUgvkMU|!wnS!)%rRAOI@j-G{nuJ3ewbzW-sG3931L4|&(r58u0K%(g%uQCC=S3LY7D>k"
    "AlhQa&;V|z?CRilgAW6vfQmNR7jnAPxoDTp9{TV62$mGYUmvcy`QpxtFIm2&`DO8|$FD6XeMw&>6G`qQ{h18fa_-1wB(JgFg+3)b"
    "|B3G!T8buEOQ_GmRitl1a8NJ<_m>VpwUb&4$61_ZH0tbLxF6xg!(>b88`T18T)+Dn&^@7OM@gSPHBJ|}6zH(B&g_;YpVgmkl&;dq"
    "O7Es)XHb!0NxZI13*v7jILg(L=SkiZc~2$DsU0eLm(4)_0HrQe%Fy?){lSid;|ngYcxmJ1hPO1{EPN_GGN6=2g)Z4;s(aMxQFpFC"
    ";F#=aYEtz2?QTiCQnR$9n^kF9((<HNkls^f9a&nkKFW3|r-?lJ60U#!r_v6p@^Rbh-!uk0J?O7tl!H+oW@G(<b(}+EicJd#EA%RO"
    "d0;f8)QYS+H7x37X|ShpgS?2gAKDFQACrHev&U(J(+Ou+Y+4$9&Uxt-rC*SwDv2#gPpzn4KOJccl=IMj!#bx~#`oYhL_34^F)mca"
    "(7;y_V@tDMw@ckfcLMx|?gXS(?3g@^c@&8?msUo4h71DP<z;96`cFNxZ)oEh9#p4LokMQ`{T7T0@UkJ=!@Gs0Bi1hHJaM!{&%{lL"
    "y9Vyocq-!MiBA*bGQLNYDO1TN$EN1?`;qtm%hj7LJFX<xg75iB=Wp~-)-AJET}f5Cx}_)1^Gt9AIL{LRf)fZ3<j>GO-2bA(k-Jpt"
    "HcLAvb8ixe!NX&D#Pa1E8S&LetuxBjO{uF#Q&wI0q4Zt|x%nX#Y$}9Q+*9!&H|h1FEX7Wb`Z*fZX<$i^EF`rgT~c&Q(ZiuAN70g^"
    "3%yhH?n{be#*)3fu;WUGD;_uZ+*$ohr|#$M%=0Xt9ClWG_Ss9aSKvp${*j+2e%|=`;Xv={F}3V@{st+5p4$SP_HbFlqlFby-0k#@"
    "#}<Zb${1X-*&nn2*549m-Tc1RkT`&OgX;iGGxXN@*#G92(V<IrK(9STw*I)bq=YTT)N#QqV&#R^GB$M_ITnu7^o7f%c&uGRw$Mv}"
    "S5x4FhBJk#Ds_eRRGG;>*>`0d)Z?qSL3^jp_HT@%qMmwZDRZalni?YkJN-}0iSlV22q#MKZ}S%|eA(8tV@rgbs>|>-Uu-P6GhnkN"
    "cRAZP?{mDL{rZ3Yw>FT)_kI5{!bS(0sUN5#Ctfo+&*8j+TS|=OUB;{}UFv0VKE-uGEb}mojm5!miJ!Ns<;6a=b!>CW4z-Djm%K&l"
    "J{1G;AKF0Ft3$bz7>KrrW=%w}mL@HInO<$f%{jM6+&yvc&Fc^E=DfSg3_m}*xqtctYg3I-<UO4g^;T?@e*McI&}>0-0fqs6|JT1r"
    "{zm~CJupn@)u3;}(1LXV_7NOBI6mOi#nKa-I~?s)$yYT~HTSCLiQk<j)%>NR8x2!59@8X8lf2M^JnhM5ygu?S!Mh{x^%fs;5b2i*"
    "ozaDoUR_;i?PHK-!lVof53g^^6ev@q>XAHc=nMK3IF%F-5v({^>u+oHr$0N_$Z`nhK0FP0b}&0f^MyeI!x@Gjd>vFy?+7h59xMN*"
    "^p0(`sqd0$(|AkMkldZ#P`N(hPM^mH&nLXf^18*xKD!}bdh#Ca*=k*Ix9}ODwMMs%uahb+mFcPRT+L2DJcn9DlNL>DoEmU>$L+Ru"
    "R{HCDM}8ZS%R{aO<t~&5P<7y<5ln>nGS=(Z%V7VCs{uNucG{{qQN^{Y98@!@#tAhs)uv8~j8MWVDXL_tk|VQ8qZLg`G+&ZeA+Jr|"
    "hps+nhMcjv_~y!%+c`Fde9$BHr+89_Ej^cL9Z-X<;1y4R!1d-urg(IR`4-ks*tcLm1g8PFGWIt(9^#~dQw^uGxLV-qf=&&cI(jYr"
    "7`(mknNiuLs%T3%D66O}Q(Y|et_cIE98j%I^Ax!j1#eD&ID6+#k~>+pG+fE@)8z1~TOkbU=U1<igpOkGm~Uc!fyF&m54arS`K~8Y"
    "Wx9F_xn*!S!M!c_9(uOqt9ouS8tF7(Ifv6kPZH*DSbJi>hUQRaYre+)QReJps*0(Msxl{P+?1J{<kfOa#WEGQ)TmHvK!cF1xs0n!"
    "WSb3_tqq3}hwoqiqQ@%Nh1^~&wZDn8J96+$!83r@3SK9ODzYw`H|!K~+rmQ<&s)*PyDl)8sDvwdSe1^l95v3X)m*Jz5yLxQWW+AM"
    "@<a7bQ>I0wf(Wm*Q)Dcu|02txb(=O>I@ajekim2}^q$b?!MUNVtf0)nN4GE55H%rrf@~FX9muVqT!ifo>;#T<L{vo2z>9-^6Q4DF"
    "E|ir~)>Ovg{!x#adK^`#mq?bfFCNW#v}W7qV9&vggW$hMAx;OlLH7ph0R$S6O~i&rdwjf5V`y!+R$h%WYSU8Jo_ct)v%LyR`}uN@"
    "dJVG9oGS3MtRE$IX+dizBYHYvV}W5-B~Qw-l=CK|NmEmQN-uNofhk8)ld=}(4p{nNM=zTYdoOgGVj#L#x<9pvsu$JpBfZl7dG6G!"
    "r=ls9r`$ETcjI2fm;QhEB#s(a(5ON)5_=hr@EXWe=_hC#u@ZV{sL~VD(33HqaCXO~KDSSNxw5~|Pxy~N;@<7)cqI4u>tFOz_w#zK"
    "{RoQ6g8xAO1H)asx&k;H#D_X#@H-G>e>`c^$L3R#etJ_`a(absXSmDa>4SDsmYH|J*HmR@!q2SMRsSV|al?v?(_*VO3w4=O;{4+;"
    "a~4&KR69_6EyLYgQ@<&`cy7^gOUE6#liyA`b{u_i866D?Dm)5=sJX6+n?4!ZKX9nWYM^`dA2Dq~JF(hbwu<%`c7eEsnRZ!a@mHOZ"
    "vizT+(5^{4rDKBJGWj*}Mb$lD^>~X$&F&i>AiooRJv}VqblR}V$$`Z#h`o0Q%Xu8yXl-Q>oGLnd^ct~GoT=hQy02em+G?~`I~}Ox"
    "sBG!Za_hzS-7jq_k_uXvObflC88_%Tzy2(Hnh&`wRD00SJ!nAl4(&bk&al<+{e#s&*++G)inL8|q11_H9;X)EnsTSc=7C2~{S?`("
    "(voSZzq%^G>JH%xCoZ~ON;D`PQSn2KzI^Z1&>z=r5#@*;bkjoIFlVqG!Yhx)5?51UD;p0vH_Gm46w+<O$u1AyY%b)hI`Vk#VK@h~"
    "1g3$8jpmN}eO`AsD(VmDC8)bFzQXB<$rq+}m_B1=imf*83i#;aJEtmV)f}i<o-$3!TqwJi>-c<=OphiGH`3f5aPP^-&96WI>5q6&"
    "=DKI9+frSFwkxt9x-0s3l9arW(QEIeZ_5_1llsTHdxsqyTudf$+}975pZ#BdXbc}Gn#zFuib=Qsb0*}3`22l&-)IVny(FtvKJwnt"
    "3D!D9eZl=9UNo`8><G&fY#*^#!~P8~D+~hs^#ATfOKP<JaiQloWkbsCIFaLag{?L_bM|ifWikcru1Fb7MzGnz^9tbyJ~z=WsD{`Y"
    "V(W%RPR-xS>yvR1c`ywSUT=Em>D^<y#*WKgp1nKY@*LLVXX-v}`*75|O`5YG&iR~A$S?Gq9LjL?B+Pa{y1A6`sL~{3P4g@*O0+zQ"
    "vVcF)AO7w5^ayBE>e$dbz`z7EDMq53dWzuIf~Swwy0p_6N%!wrN~S1zr~FaG9uGZwHYu2K^TC}Gcd9(t@a)3#F-Lkv|5?(7lbG7d"
    "U$r$wV-eEKkD#RiT>@+kQ9Iyl^pIeAgKZa$zDPmseY}k2ZF;23V^wvP;izttl2t0pQ+33(9>d2MA**r^N1-a3s+?BWh6*AYUPY3s"
    "W&ir~@1&(8WKxi`<c6$0l1T8TH{rC}Xk^!>+=t#C=REyk{nSnhv<pq>=8CV$LTN7szS0K10>25b9m+Z2iPA?}u6WbrowlwxxqjFc"
    "4erlOWxKHvN+6NiK_(EB*2CMa%OcOOv9cGXh=v9PrhNS3(BVx^AU19E^}1tAPvigo12n`*ZlqH;TV6NIaI2$#@uxpjccSE$xbaKB"
    "qP>Q$0fYIkI10T(C6L>hMg+|c^bJg8us*}>7>^q~HPF7w1&y~vf2G=vGAintqlEU)9XejfzH&0p*Bbk)U;p|~fBcAGclLUd5Vg_Y"
    "^4Ft>NKO}ys&QAR`H#$Xb4T+5-LKqo^fyHq`6B64of*9QqEcFG=`V>?cCn&dS7dctHToR%XGL}I3Wa5?O+ALX37aZ7I}x~z);|i>"
    "<sljg+@$ehqc#-LFD<LWM-_GaU`SH-P($FpGWCB$Nd;asM87o7&Zcx%1WOxJ$;h71X%(j^KU;#eGiMR69^a)E;^^9vi=C=MtsyA8"
    "HxQs5j77FOdWdp$;Un3=zcrA<lv-pdv6tunmaQJAe^P#Hr8phC!0(2BvSib2<3!y$v|_dDKnE_SXj7NVBBXnni7<7!s=8-2bbqv|"
    "Ge^<VZOng!sbeiw(W4$`M20#z{rXo?pN@8-KHc<XoJVz-JSERtWKj)C-tFZ?XxcOJF;#V2^0w1M$*-;Cf0DK@Q@R-x&FF1#w#vCI"
    "8wVcj*(&JAi=@*Ix(^s!CExnlmXz!EL76$4Ir95B`9X%Oe-fGKAfpEg6Khe5u5M(MtTN6ExESJcAFD<W@FC-d@0v<K)l`SaFEw|S"
    "^H8@1<+fBxQ~62lBTY;h#+#1*>%ab0PRu)^DgAjvI+Ud6QUNN@A7$yr8ahiDykJ;@;R!7JhX+4d%4qC8IP37nfFSW(SsIH?EIzSx"
    "kSynRPjsY3KDviaUyl+-H4%<(+?3H4tLm0jSI3iM)my7UR}J-Ezf#kd$V7W-6}T$8s_3EKIk9IEh+c{`v?$?Pk`F$tYKZwG4SmwF"
    "qapgwsSBqMVtMT<_vW$e^Odb9Pka0t=-&PaJ}2v7Zbf&ym=r57)s>SMG3U>Y(yxDrIUk1Pj!0YiZ$b9WoVNqs7kJ;3M~Sj#1pNve"
    "x|nT?tyx!5%zmYGPgQ%a#uK&LD*vfoD^YK*?Pz@CMTQss|EbcfA0-L0A8ctrq;YOXzCB(^ca2E@Qd>%;1MMVi`mim+!GPmlmTdWj"
    ">x77f7e|!rQ@>2!hOP%uTAsN)Z}NOC-<Flv`hKi91OEg~2S$4_s98ducCLf>l7YxIaqRv!BGtZ&`D~W*SZy+msyuwxAtxgV_I8@R"
    "+^>I?-glvXgV79jIe2`e_o9Rx;6=yfmEU5>s-F09Ak(Wl`pWBw)5w)XxcC!KRIbOZGPfQwSC%gmHP7=YqhIhJ@f#W|F|}O=ONK1!"
    "--+}0y=s3%Pu^c}di(2NWi!c%o^f#nm8z&34|-swMbCITi>>nhBVcS?(YWEj{!y;iZQlCm=J2+b27hdkZf=bw$6kAij6((O-+GcN"
    "U(1Oab8{ww%qJb1o$2}FY{2C`w*&4hxw93i<A0L{m5wR%<8RQ8S@TI@&3`68l=*xH3VD&_D=$T{v*AGF44pP~CZc8BPr_geRtS@r"
    "6u)Z1It2Rz=Rmk5UIVT*xLH3$_*WV}cFZnm3mrNY3Be?{6SceIS?KVkoA9FRCo~-F7O~e8BK&BK;}K3;IPKzWiL)0jGPo$ky!b6H"
    "JY0W7EbsWlV}Pd(x(2#EbQgFwBz4}a%Wc*pkB=4xdwk{abriCEDyeFH)mW>>Uc~D=YoW|fD{7up^NyOEYOztvK&_G@7P=~^RiM@@"
    ";mdC$b*>8~^1fDgTipX;#D}(e6)B<DeuMH=Dm6t!^f%kRQKn`<omh*|yplDhWrCI&vJZ4F(CtC@kMP-lXzV5WWJFQOuy~#jmio5F"
    "+dA(Xyl?Wc!S<G24cMyer#NWE%=CtyMWOu^g;grdpOX+(E>ldpSYFDA)*5(?gpG2usA<vs!Q%!WQt~}HLw$e4$v0YkKL#XrkS+>&"
    "r>M;`4LuX4O`&x=Tkxuq7Wt`T>V%mbW=ojW4rz@w`|oV-tK>&cnz$(7a*CS`9;bLJ#*}SChW`-=Ut83UO^hs6)V?4RruM0<PH$z8"
    "l&goftIkX1C6#|w!BV}>lzIpYyHujmj4BpYx710}z@Vi|#{r$L<gUrRiu}MQ$;kyLA5vqaXmaTkldNCdY;&u|ttGe4+`4c(D~zT7"
    "jQ3*>4Ivx++s{qdQ8YN4rcgF|IhYS%9>U@L|0h#t?^x(yp@*fpaAX>H9Pe>iiG_6?+%HsW6vL66aA)j>dRNrDr`{9F8e}|3hcVWI"
    "R!Mqf=y9NDg`P96<U~NX-e;rB#*I6BX_F4ypT_sS{eJ4gsxFqGloSs2Y#?N$%Mvapxb5Mw!Bhq_387l%3@n>ijRg@}M<GymJmFB!"
    "R=B<4E-y@KI~2B(bNS&=k2h-8Q?sF(jnuxX_KrGamHiez+|yMrPifHoc?w;+I{x8GPb_k5avkAEKORDeE)BVQWaG(Zg2yc$>n)?B"
    "yYQ!&nE8r?{;a)-@rtF&%-B!7VW)AVgXyE#lD5+7Ll&!l&}&o{;y$eoC1r1vQx?_7N=7n9?io&JxVmD)W4EB+AZK*m_kD}q(*k;X"
    "7z~6#?2N#f3u`#9ZBuVn7i_mgta*MA?(n{bN9`Jt!XOULa#7c~#;oB;ZPrxUQAtCOB#qlNdkbH9ou>7kTWdDjyzzNolQ#?G@npp$"
    "-jI4jz652xXg*+-VBtcL!*o_`rCmb}nuP5;N7u!NkA7AyjxnY8EgmGwP}-t$UwFIgJsrw)T$A&lFrrt7qL4>Tq3%x8JbSRyjaj-U"
    "4p+Jj@$J^pRN1*|yi~iSb`POy?QM0+Q>jLkjtDE5M{Q`F(kr6I<Ej3d@MZh48gN#^19J%G93D=r2Asf^kIoU@2+t|JWQC|(a#ceI"
    "NM*Has7p`C^Nk%1jx=<HqPysm4nq@7PPRC8=2XDhGMDZ=Uh{Y(Bq6uLZj~=tA?@C~{EA|-;z;<4|0X=L;y0Q@>G%_@)Iz9tg{!;0"
    "3&Ydv3N!bf7jkZ)f~7Z_o~YK2eTiM%sxWO^J2h~GUAyqq%2c<E5NSJIA<t@nYBq%7Gkwu)PV+sjAGB!-LCmSqz5Ju9`gl>`P?!+@"
    "Y;noGH1}ISJlRc-HwE5hd8c9ggME*KD+f;w=lt4n<ng<#yYZt290icwLcxd92+A8M`$GM+3Sia2x?sx0vJPRcLY^I^aoYaj&#vCM"
    "IpB6DDhQV%KJTh@RHc(7QFdyoS5l{nDD&PIDhx$b*Lu*-CTA*i+ILcfbW3e+Z+Uj*`G$`L_D$WSKm8#r+UiwOFeW+i8s`G04w84b"
    "e&9I}-GNF?^?f1Q&1^LfgaKB_h_K`4qBkVrwD$F%fvjp#AN2TH(ht@U&(uJ7q27A(2cpoHn2AVROho<l@4t_jaMD(pyUJ@A-=bl_"
    "2S<LR&2{*IMFkd1IHh%Xui8tszNp<o?Of{j>Ew~!CVR(CPk&ycwrviJuACsX-#DA$t{^Oc=U9aus+w|Sv}*C{{_EdG@x7wMYSht0"
    "`lNDaDzEfU#Xkh!ApC~OZ#e#jC%M1Re4vnq#Szve_*CUYqNtICqc$nEHPp4J?xqS3RIO8UNVACMH`-UpUQ%@6(TPWAo>X|U;l-X;"
    "d%h*)J#s5w#X=2>hFV6_o5Y2RpI`s}&wpS~x8ygWz6bUbZhiUOG=u97u5AqNDrczjR^2rEo~W=TI`I{sstaltY4oOXm8MOu8Qkee"
    "O53!epQh2K#_n}J%!ayTDOIFrimTdY7r*{p*!UO7v>>~JVHQ>wSj#yEoC358I7;Gqgx3oOksLVaH$Fr4GUXBZCRMcKYf$$=(+(|H"
    "v|7{q%$Yj3A8aLfcI5erFRp&aKmCD~m;C?Ff1p*8CuxivPAGD?Z_A;@zsI_PO*x{g&v4ww^GP+wYV=X}I+biHzl0XvDN|=j{S*zg"
    "&$x(spwZA@)epU|F<NrB&OLeE|B9#gb=K1>m$0iM268$YKWIg*klg{h4&VJ>f06vWEmX5u@vzmBiV$`n`o+;rC7YC~QDZALI=VZJ"
    "ym;gb^Jmf0Uz2oy8~Q64gfJUpLgSHuvNl~Jx|uvHv)krNT7OWMdhz=jL#hw;Bxv;0S)g;17hIC+*=5V+bz43tpkf2vJ@nex(7SL`"
    "Es8v!>0ilN%@!sR98yx2qj<nt8fzgo44ltZex>?@c>6u*9hmYZ-nMxA;Qdf4fLzMI{-VvjbA(=Aw^V~O%p3&+YoL4Ip<fn=`%d6H"
    "shShDYpUIhiW+|!+;;Rg_1G?K8IaJEf5pW)e*Hx>Hd3Kf;3(FhwidAly<K-!(96J}24O*lv$m3Bzfl55MbyhzkE=IOSsfB@)HEgc"
    "D#4j*HOUTIOz6_$_KvL@pVsV@*xSlW^lBKi&^+qzs*|aloo*Rt-|{Q%HZo->Orfxc;!MPiyBSe7p44GJ6Mf@N6*fI7s&PI;q~oMK"
    "*2bb~Eafyd*v^W$u`!UgjTSh$Nr{qu0?#@^J!0gGzHv5E^^)q2ehO^t^6J=E&yETV^|ICbPPvN+9T(<Qnn=kFDYjwL>_FQN*?YRp"
    "=rN2Hc^z&wxSQr~iF<n<ba*h5>Kglr)G<11I~m12lKC%vUrR_mpp%2~=BJ#-yanz6+#wDsXy@^~#HXWjHx-^~aiPtaHdnfg=~kDg"
    "Na(7e-$_J&s~*&kVmWT}??BHIP2c$(0}mrlr8g?`Qu&t3f7G_Bj!Sj&q)0|srDE*a%Ni2+w3u`1CNOPsXZxw&BAOk2s8wM#2RDP+"
    "CT6dge`Bd1=a_9pSGJkLW=1X&wGEsGvAS##!>Ou_Rp+QWo}`e?Cu;eY^WZ{-3RNo9s9B`JO)4muW7?j`erv;^TZwLKPVKo^_|YdF"
    "Z+X(^K*sG)|NIB-U2>E1cj0R!zAyj#nf=t4X?V~Kp`C(t1e*Z1Q`p_%=z~22XNQ>=W^1B%T?(;3)LtEzPh6LzILG6Rei!|oB#mjv"
    "mnf@zN!7E;7%JnU`Z=l9G0~R%P^+=5<m*L+Sru(4^_IFF*&5aQvLje&Ip=nCRGpG+7Aw!rja^qS&A7oo6+8NndqHsrMHk8=sOE*{"
    "KHdp5+_YhTgIfvvdC3SH&(LqGqNmCgWlWT@5@}-7R8~@1Lv<V|KO=XB;f0c2N_sI^eDPq+uf2Ys1n}Lj-}?q-8K|)HeZ9WX=liZg"
    "=>z4t)Z3^gp=Lnci#2?j(6gZ5hrtmhebJ1bwP108<r!8d*px-c+}3|4U@yTjz*&P6fa}A#3g;SJ1|l2i7r;Njs}8RwL|sg#v3wFc"
    "k?6gFT^+qg*t=uj!9hyIBqtx7hPY0m-4&<d*72;dcM<O%J}rEH@V&%ON9d)gP?c>}%}V)+$y`Lpbr8M`)vB&ewssfl`IIm_HIJ>U"
    "LA47tpVTs_U8TX9h6!P1jkYvC(7H>T9PQS$+tI#FhZ!A*bn26nBd0@7kKC2i^Eh|NbLn~!ftbgRq8GhGVU;IFKSI^%k#<m=FR*sQ"
    "`7-D4VzkvFw+p<O{M7tdj(D};mCGBGcXvKr*zU61XLrfoiajxmSZO$ddQHU24I35_*adJtSS(|)g0%t~cab2^O;mZOstZ;1Rd1*I"
    "Yjr=W@J_`#ue<s=vb(RppRttHNYo*_hq@up>qRho!#oR{kwE8N1x_xU$8bJEaK~f^Q#mYDvDA`6AI&UwT<rKbY@#_p(?`oj%R%dm"
    "qX~{)IG*FQiSvtG7Hpqrm(aeT^F;52_oh_h&|7g-$9GlQD@UU>)vc?|K^?m4uvcddqi!mAEA5g#CS^BNXh|8SdPK&Nx{ee;=sn0v"
    "(5y}88l4Yx?b5AAFQ49R`na5^bH2t6Pl|M$U4Hl+R{1sM*G$Ghw96h~<iK$8Zd0PlNk9I%-o*VZ_$zE0LMSqJf;J}lHE#TCy~nr("
    "LEsvDYsjtl2K{aS$=ww9cE3NN_rrPxW)YkcxGf4b@G+1Y9Epu8L~IV&vgCt$BXh6l-qdKOwpq2U(f%l2%eiXD#oc(OesquRxG=~J"
    "lPO#|qHjDtQo*9mf~Ps2iERkH(cZmRm08rwq%r-O9?KB#zA0hg;Kxzvr1<qhRVcBuj+*aupiZqT^;WdsO9Py%aGcwjUw;w5F#3k8"
    "?<)|G@Sc4?lkev!70j}oxR{qM_vUq1YHhSe&|5(N4yFm4mMr*(2agMcNeD;q9%wLxt&S}0aYJfwoI*TXc<JH2kMD|FC8YYsdZ#u?"
    "b(pK;N_ZnTPX!b8s!+nBREAPHs;|hrkPZlqJDbIL@mzWF;+4mn3GZ}lb?2b>>n|}>&gY?#1-lH_4cvC{*<&JwsUc<$IC|q_tPX2R"
    "_>{a*y-(W-?bl?#NIT+0l50;5l6juurO(HlexP1@^$GO4GQ7Pvgk`K+vU9e_*bb$7P_K@Qhgf#tsc}`CFBSWus?xQ^QpaUBUcBh="
    "y7cQWdi{NRkiI~#C{LU$VAh1?0PGKD4g$m)N7*CgSq?_pnXlDWM>iF<UnsYtJX3iSb)72TR<9kU(v-?lxlZj585Qd1so$oJ1{_y9"
    "cF8G{JLANJlQYh)c;C^l7r@<eC2!E~QOnYX&slBzg0^*AmL+gM;3XmI2~IwFc)H-HsPdawMP?#GGE;3iHMP>vFr{`qDdcglt8hit"
    "NWfdWNrQ;oJSS6}3^<kMVM)}khjkuqc(`S=%ZCgfG{BWePzu@&;B>(11RGUcA5_^>$Fe$Ask*0jf*uPV>cQLavZ22rJa98!xbxWD"
    "*RXAhLQXLu^}RI2oE>m}l6GrkS=y*-s2UB`Tu4@=+f=UhD|6+yevh7dZ+==lb0r3?b=gRYve#+->#q_9bOpxtT7RzzvX``aTW?lv"
    "tc-tZg0%F$6r$J0AjFp=yHIhesyg-#q$bGyLw=o}WqQA*8c1?3zQ3k@zs7wl?!6>_7+X9^^QER=spmj1sHOoUTWW$Vwb3RqTb80#"
    "?U^i`y@O{LFRn_RDHTz=CIpRqKxKovPbmoU$Z^W$>{zlDS|-oWKXpLt273+t29Y3(D}dCWf{pt*7*%0p!e}iBd0|Nr5UBvtz{C@4"
    "b8MD`+0oZ7J}+SSR71y!u~1z2BixBroxtOhz~f|JrD`g5QaxAo4{C5$^NYsGQvRc0$gyjuK=YQIh8^;B8j@Qe_e9>Fu6s^vNF1?o"
    "<jzc*Dd~iLNUfk$2crS~8B9|!>x18vf^m(#<Z~^iQi)81-$zB`UkvIheUO?U&6B!x)mul<E6SNt3S``)@#S~jF$x0CE#iV8SMHR#"
    ")8o}fDuetr7P;F(OGE7rta`8+g6>>47Z+ow<1CHymlXTB+ITnd(Z|sEDfw}(sGBJzt?rHr*VM?6cIWn%9v4oerR2v%Pw`R2fL9%U"
    "^?v<TM_-kX;Ga(uJ}u0SRPjm42YEiXBmHY_2cMSyza9n*JF5Fq=Z5m<lzmfgN&6BVGrZM+ar5i1a&g;^_i_u0X($@ddBLy_Lqo8c"
    "24m+UoGq#3k*Z+2gN0BqxkgzUT$vx7J5tQ!I>CEKs&|a?YFkoAjrX_el^3U5ZBln2fz0A1WjUn8LxzGX1+Lh1qALg|SLMoqjiwag"
    "nD==*<%1!0IP48O556S$W=Zw3XF)gjcR3EJ#nBi`dAym1j@mtsFnPq<NfPwzjMUtCg!t6aL0&b_%CMB-3aT{%)hVk^Q+1{qY*RM2"
    "D;;MIJ90!+_=u=z&5rAD^n~blJ@6wZFa2HgMW!|>^Lkv#q*`qp&Ek0-DJxK`O<ZTABSDX6smq-*Ny_Xb;%N{6KLt1L+27SR=rfm-"
    "Ydx=r1AA+J%e(a4rWa71O7%b;E+l%G7*NBKS{dGpu2(K*xYXgsOiE<5lf1O`m*T_V6C^FDWubAD5*fP(1WC|84D~+xz)B!h_+}2y"
    "(|AsxKT+wlsz-WAD0!u1M8!PSOevaSwWUgimE(E&*I#4GN;vvPlei*=4UG!4)6lzt=}9e&OADf<)WS&mnEYUNg4HaV1*wQJyW_Sj"
    "1;nmnDIC``)%c>OFG;RAtkkKcZf6xv;@WX9ns}TT{=I5kmfIG4T`7?9S7F%&Q%F7_>BI7hnIe`t>RzP7i4GTmX^9?JZK-@QJ@MZC"
    "^;bF49YD4wWij*GOd4Qbz<h(%jcZ|4r546{3h&lW6^wgItv_m$R{Nwt#M+JO6RAe>_;AAD?3G(p{ZN5!<{Zq06k2l&5_Xg(Qp!P!"
    "PaN=BRTW2d(;_yv7z!NI?%`h4U({>yHIY?X+p26JzsZ~_`yn$Vt3%7Z6s<@kc&af&#Pc?<^r*)rtj1EYqH%?0Q;Jrc9eLC69UqLJ"
    "(5w7Ztk@^i)sTv5`Gh>|Jd{#5iyg1F>?QP*<Ps;ZccO39`@U0PykWG3^-6C;5Im(OMX@jYQMoInC?+FZo^aK{O;=J1cJsfNGW29u"
    "nl65KP1##PwV8wTq>z!=dfOB9_o&flDTONZCZ%_%-&HC$8QzR|yOnYkyH{LZH^FXKPAU8wzpB6fQ!WpekbjD1;WiCh58hR*bVZ%E"
    "y;d4nW+Td_MGj$Z(P~epWK7B|OX}}`%AVJ)!Lkd>6I`!&+UuYFIVPO?zA%1^7F4TXo?yKZ((kSqXHlE*av-W<x{Q?w2SXgJaIlk;"
    "#L*I$34Fd)a-q@>Rokh?QFWf8A2{!+V@lcDPrEAEs_?Bo2T>8scBqsQN#NRs>IrHbg-vgL=v)z1;8&Z=fk*;RwS$YKb+TAzlAgWJ"
    "fIA0w4^aT`1iaI_=kV!cx*v0U^pZ6tTl>6MwX*04-aQpHRb&XW{$1vi9z0>*_`x@*30M3z`M&nws0s<~3yP4eiFR;$4Alcv&q8T8"
    "9O$NE1>GxHRj_vA<TnOvldx@rTLky52N7;ZcvQi!gZ~hT!MBa+31;$`b<rxKwHI}`Sr+Ga^mcgN;C+hz38Sh?St=E%>_la^Dz{Yy"
    "PZZ%QD^)qFnyor@)%*OY2`-wFg?T@zhok(g3UrKlrF5E##rW(iFKDn)qvo2L7cz#_%~SV6qY2GSnvLn0B1ikuHig>eMx3~dnqaag"
    "h_QF({+x#e$rT?yMPG0-<H?#Qd!9~tdE--^?J3)Pb~9p#o}ouk5~8b6s6izOjSxC@Z7Z0WV&jJ0ywH!=p0JM3cl37{98@YNwCYx%"
    "`V(cn)b^rwb+y}x=I6Dfu4NVI4S7t%5-rEvv$+>?zruqCn+~s1?Ar294N`hDSY%b(Rz^pClGH9y=gD{P*MCalkDkOfR5wDY>s3VM"
    "Sey8M56=u1nphbLm##a*Q3btpta7{(p<~^k*<S5OYOm4#gz_t@U8!*+t4A6vMh|{he*LEe@s)32XgmCd`p@nAs!9!+w+-0@lxC8S"
    "QT{-+0F8_IpvD`T#W*F#65gsbXeb39tRsxZFq^}ACsgIb4IJ&N%5dAl;{bt1CreDE<Ek<fkzCE$SSVp}iPaQ#Uy|a{o8n+8eVA|X"
    "v__{a?B??j&$C!>)e&xBx~?(}A$k{gs`yrQUpP?XgbcQhxpG%3jHzUau4-jLojq9vNdwt0Xul*!2c-^uI-ELkxxlTh7>D_m1cq&w"
    "_a@&Oe4Fuo$Ik>mi~O|s<#2TT^?!ueJpF!BknTd(hKeuAZq*PPac#)fT8IutJ+2!YKtB}Hmsy3S#(qQCU*K4j^Q!Ygv=~LTCJ%lE"
    "{F6kp!as2^Rm4;W(^kwv8T><yws@9Uw_Oq~#=e1rF%DcDT+m#{(XS^&l!b@eE$%$rXYlxt(@-ItG~`tM+QZuvA6fL<LO6*gKPOvh"
    "M`b%Imr=Qa$~{#vP^E+_O(n>w)FjTS1*#RP;Xr7MlSs{4YF$!m4Qdi<=c~Q1ym2hY=!)`Um{Z|Wy>$rWsZUFNI+XG$lNEJ?+(<Ts"
    "!duu9I^=E^sOeKXM{Sp^6<Jp)hdC+J{3MWYd8d^}s}HSbv?<eeE`@!LL(y40Z|PZ*>-Kby(@V}d5|fmg0u?I`w-;>8xUb{F0S{fa"
    "G&bIekh0U1dlJ13`YC=r^epP##V>!~dM2kLfmT__?SjZcCqyx^`4UZNQKim^&dM+k)+1I=e1lhj%M31&+&z?DQs{e_7k$KeAa@VW"
    "MI;i-HB~;T@>4ZCqLMf|t8q=ux*}B>Wknou@D_1IH=rm(Zw+=Eq5xgBcwn(LX1BxPRN5$~%mX37q)U)3$jNQ(BXZy^U+4<!UVQfu"
    "!1fA<3LHYP_acz%PQmNL-G<j%E+l?aVsf4+h1U`ief8fse4;z}v5R|6(!8}T<aNtOs*+X}JDz!0)t;*URy`eb<<+1TpZ!+UWFeY`"
    "tDNx3Pg#*IJipW}tsX<=FVwRnYK6g41t0aEsrOa{3&jKiDm<uMmff&+r(T=-LmD2*%F)dDQ7gR6>AKXRIX#{tNtktIVm0_KSu(Nh"
    "H5*%r<+@GLBkXzNI0q{p?0E3tK_J?Mmdmpa&m58JS!da{MWN8M_?Z`N!tfc7s<8QHqKX|HVx7!7)aKA=Lc0n>8y*@VPa#ZU`XWT{"
    "?E-h&>6G!=SM{}sm72a<t(1LMueACYQbV`Yqvk;Fi6?DZWoUOHXHDlBryrata?|6^T~rAtAD(7;_T<HumqlLb81cdD0q-}WgMGfU"
    "H<tEERPhpajohz1<%p;rTV%_FRtW93T(q?9SoXdQ9bYO_4;NsjM7;1c0{aGT9j?otYnWCWI|UrHahOE2h~pEEZ^FHqPvm;Wj_|I-"
    "?i@yWm6)hPD9VMEys&PLO%W_Sot1qM;*B#^?m@&0!<n3&rn<3m;Z81ZDko}esH4rgEw?zYG$%A%f3T5YD<c<#yK1aQnB-G|uVeOY"
    "-Az#<G{3=3qz7{Y$nBu)LbVQ61FAmM@39jt$ckGv!8yRW3FkJ1Tg?AmqqMfdMj%outp^&Gti+jta~D?zuB*5{iMZg=$5UP$p&iKK"
    "{w0H#5U)Go^E^jVz$S84qNoxJmA6IBy?PWNa`n_LCeuB%85dNTRIei??vzf7C1>&?ht{1_cPX0glP689G_BK2L+wx$2M-n<Hgp`3"
    "+aPx?r^t^Z1!GY^O{L^|NqZ)ZQ>V#)$PmVb5%j3Y+3XAk0hkkTLU7CQNy8@tpAd6h?7G<R;BgV_`{*dOKqW_Rf4X~`WyMRrP0lB|"
    "VQ^Ci&K2(HMX;5p%DIQ$uC7qfR^EqkS#EotK16d&Y%mpKI>7c4`x6}1Rkkd5K9!v;_o*SGOlga<D|ua212ByoIwBXp$Vly#T#H+M"
    "9u(!cV_#*vu3z`Re#C~ddC1?Pl7fn!$A<P!n4IAGiPyWzl_>4YDO0J&W=4NUikmdQpZWJ=i<S4XvT@oU@HAdnp%qE(@9bJN@@kXN"
    "ah}qV)pMr8g?f+03iBtbO=;dGFUzGT*H>&5c-`Q`lkF?}!LR=#0b}j^O3Q0>ASk(|J*W?$k&M??C`G^J{;4hScRqnd7)xD#;QoT3"
    "4)2Ck0+%g%DTJR+6+3<Gov>#M?Pt1+t8*K8>f`kwdhkIGLtk3lv6Oih_2^4MGN$fB^=JvVA*iTuufh{0CqgDnuc%a}c1BKI2SXZ|"
    "w8%?^jqN#3dhp5FkvkUS$@Vzc6cs&lC*+4|foCDlUcBz}amc3ypY`&JnLj}vGI^o>G_rAhgN@9B=%_C+b;P_c!h!V~wp`K7>y^c}"
    "h8f8bS*=Ap?|Q>~8+{MMGQM`Aqn<j50C={fvRzfn$nMb0s8(JmUcIy$EY#pA2@cjy&7ZO(+~%UUr#FRgNR2aDHJY{P@S@Y0+^+WU"
    "6zw=sl(XVghKof}S#KA)GvQT*-J$NqpW;hq?d(Kj&w<k#PCm}MKN5PEJB9_}3+?oBG-drA4RV|0XDO<%x#!VUK>oSFmkvK7{XC85"
    "lV>QGp=?6ggYpF?a!UNO)GlRs%-|8gBZ7Ax-g~(lNtq&yXPR>UYwMuPjCai&-VBk-i%z1Jm#eFMC}-bQN7WZ<zg4HI?mSf!B1T@_"
    "iXQpzbIR_4x-M-?v~AOFPWFjxhnzWmvYaUW5SEgbD9&egoC{<fudlpw_+006pS{VizX@_Ve1qXPu%yJ}YYw$C)Y?!xL8BW(@&vS-"
    "V05LpqqTwYDX#3;fJHkl?wAz_bM3&j3-6ggnEDj06V9e_LB~A0JG`y%?g+jv4pgHg7iqn&8V%L7spbnRMKrW%HlXc@?ECMcVf1N>"
    "ZC2{s7)og<Q}1&IUyFR*vcG2kq}wgw_v#zvzMt*)6F@QznHOZc!m6mmwN7dks4b))wF|T&Fp|*G)4qb?RoE622c|B}^y1W5Is<DB"
    "eSA19z`laFgX>z-!0uhaJ&y`Jx-ykgPUi;U9bS2eR+6iee8%jI9o98|uHzuYtu1w@_aC$$=q}Ow;58qY=AGf=fR86WZ44*)GUO0C"
    "JyDsW$`~r+s?1qs2P&UY`MB0e!&QTdG8+>2X|FjQs+prsH|5-w`%uwVgCR<vC|jZYfQlC?-l_PY(t&D2s$Z$@OHQ5DqEVk_RXNPw"
    "Cg>25T_Jl)u7<uBIvaE?(``ie7bgasesgBd*+^K$!XAEdH<SXJy#x1)+^@xzG$TPpW|Bu+p4DYe<`yrjylwNY$NLJO@_g#^<-peh"
    "Khpe+IGpk8DbpoKeC{`LAR9vU2(2P4ZJ`{$i<qcma)8An)`!^hv7f^J6o-8rZE^a*RS7pSaI(|rZt!xB;gf@}w@M{c>Z;P(gXqwI"
    "sOpyL&Qz}@rM3riwb`h5iOOd(`x-=X|H6ZatpSg-yezTX<f}F%N4HImuL|EN6lLd>CCbi?TrB0>hVc-lj)cUb=Bz`mGq@XYPjFdi"
    "Oa#}rWS~D^@IHySb3KFg02^Iw=FzlpbP?4Ijft+do_v*f#`l_`s@zq5qnb<Ayes4UJGD*i3u@mKV)8Yko_Q6lDUqPmm9j1s^tyMb"
    "=}`A5m!Rt;9jA1hk-H$@r6}O!fRiguCFGp9>2hl&Rh0*KUUYdm<ZXv94SuBfIsWxGAuA8R&k5x2f{Gf3ERF66`g0*bO)RO|I?uw="
    "5`s)Q!dh2syY6BAE|6$o;IN8j22C9*-f-zk5$4q?Hu4nSJ(b?7!b24TjdcX#B=3|h%Yo-_g3MHJKS^?1vba-$+<oV@kM8qmB^IR-"
    "%<RU?KCg7N9Q^v596BYw{^$EleV@hebD-P(4c6b_6CWq6zTy7+D#@8+H2g;I?|URg5HbnKWS~%hQX483RJ66Z;sxXq_$kNM&2}7D"
    "f8@AB_jzytvmnLoEJsc)Cq69Buy{(0y*$J65vP1~V6&Av2zDB585%jlIzQo(k=&2_QF3R0Unaj&@G8Ko4ABUpCwy|4m||jvi4Ue)"
    "n5kp#j`>d<f#<N)#LDxhI6|Y0?YtZo3#+7y{WgvUIPK$fgUc-LtCIVnA&--d^F2~A_RI<P6o9Z21RBior9<(Zl=v05a*l)Wsmc$3"
    "iY9bi)on_BzV1Qws;b{p14Ef5Wep?<o9)%IrIrJ=s;Nz=j$P%Zf1vCABpD!nNj;w`tf|*L4#jj>{!yQj`c%~CqCPLV08hJ=*GO_f"
    "#U&L3D!!?lrSgo*bGf5ww5eOC-iU_Dm@D;;1NEuYM3^mTwxrojBr6sMElk>8<f?GDkXU{HifJtlom{eQa+>6{$XSqEqO(a}MAuLf"
    "OTrgLcX~hQvy|%I3IFG?F4g30opXnOFk4C;uIYu6=k}R9E$*B}FU>6SsL0bL&w4y>{9N5@TU=dvljWVsyN}!!*;{rN{77;*;!y9P"
    "g0f#dv9iWQ?5p8Hw+ZG{GKPLjmQDw;K`mhu!tMx87ThOXC*UU`yuz!483VI<f#k>N?;A%-)|B2do~CRW(MdLoa-jNeHLx`#p!8BG"
    "{FNb9Zd5&xaiU>cocwmEkvnI`qV8QvaC^e-JNNS3zw*H4VS|TL9-i6S^W?${k5~4Oe(K+1T;C`|vnFCNZS5BB&|kvv4zsfO>fIIg"
    "T_JTHT{vDLFd#a^$HIIOTN&}#0|$pSoLO=Kw0h$9jE4}zIYv2^Fjan{nx<;*RI8@ihFp~OKgujf4aQwf`30eW1#R`(swkzR3grTE"
    "-4#PZgz83AziUsb1=LtlV?~WMwO3?TX_TkYhsJZ7X6afd4U@tGeQKN-b9ye`etPA3mFHc)9{KL@<H`PzpKE@)zy2l!)d?66=z5g$"
    "^uObL?Zx-ggwzSjd1&dVF$t4A%!{zd3a>f%eFc@m><!B+Y$wt6@p8d?1)m#bY^5}Nub>8cA*4lPwDTnMNxLceCGz8B<5Y(;eeRfS"
    "R(aOq`G8jjJ1stE`7+RLkl3aFjhf$g6Eb_KE}&k3dKu~$abV3gm>(EjV6_5!6`$-cWe0jj@X@i?6mu!zlIyL=@I{hLj?&vW8RBY="
    "-cxcNd|ws2s`Qk7sPd2<$hfNRLaiFI@w}Vr-J|50Qb&n|YL-MoHJ^qq4X-p>NHWB}PtKWMcTSc#Wpnz**&^qQ5-PQ|$#vK+^Z6s|"
    "<8}P?H%Y{(eS<=LeNc~MCR4{uF}rI4sjINNbl4<k!0&i2lF)5HcL&`N26M17U=<~bnoOk6>m{uBur*=p!1gR^ymnr0TAcW1;rxNi"
    "9<C<bYH%~-VCxOhMxbdbhna%h>}7_SePg~P6?)1&8L93|$OHYe1YWa&91^b*xSHWQhsO~fmw5EhX$v##tkLWhA4_~V=o{#d@Rf`y"
    "Vi%RQe(odkQ&m2z@<mm3OjDFgmD)fx6RK&&4APV8Cse<o1}$Z#)XY=swOT*bI#Bzg+F#TmQfE(jD|OAOU?yd~k{?QGEZ_;lEPEBB"
    "DkQ0(-E3Y6XFAR+H|0i1W4?w@?S))qbRN`A(qK$hRq79p+5)f_9op1rU!?t*4yN2~o+5Hi<R-}Nh@#o~Ab{faq4$eEDNek_T$4{u"
    "^*Np7OqN@&Aj0mO`vo3m+1yIhIBmpcoZ-bG-n?~;k>K@-cPBoi`FIx9^-~;5I-ZUu`TXL?RJd->96yg79r<0<Ruap_CLx=KTn=(e"
    "s7;{R7j)8|Nw{gAz{-Ku0;?St@>)nOuh~&9-1a6MG9pA}o0!;Q<|ESWwYQY>8Xa->!owK7tVExW8~P>mJNWc4XsCj#DqB_gNC~gb"
    "Q(1Gh`^e4PW1;+{di4cQ>#^<j<W@+Bo8tn_lN4p?-Jy@g$*J7A^=%%^dD!8Z2EKQ`n(WWnzwoQ9I~B{fLZK^FwWNYf57tTWE^v<^"
    "nqVr6skP{qnl74W$y=D#Vrj@X9wvep3sqHHsmWP+OXb}J8fsi<b)-sE%!8wQi5_VRs$3jPT0wLAga29qchB7QxSwTz!0)MUi-hpa"
    "?`tHZTn<ICU5~FG+)-Zcdrlo$MRGa!ltD~Jaxv9U;6O+51+-h}dgx`)--(HrlX3%OzSW{7vFT~5PA_$}l=gtZNxe#x&|~NS*kwnD"
    "(+TR!B^6!sbeqtp!O1eG+nnBTcEq_@@O`kAK{P#{9C&i$N%-r({^<{>jG&c*RvB7n=xDFdl5AWv4SoWPH7wt8lu}b)ov-R@Q{G~;"
    "$)?4pi~h2PkD&p%Dzvrx*n;iCHNs*Qr;$qPKW~-pD&tMBB1Mt@xailM^$X;oMhp8R9Hwx1h|@aboX$_6+AZbo)kOzUX|jiOG3i>O"
    "+nDZWuDrRvmB;8!bk&Df5epqvOi`vz`8MSzl;2SENXCtNb-K3cmge1!4=w!_y$j_(U@Ty;$LtDwI~*@?(ZfxI8fPW%k|{AvNy(Q("
    "S{(}NGEq;LszvIBG+feYLP3%fQ%-6F`26}WZF6Df``$qE3WWg_^D=y$4fMO<hY%*QG{*7{YkL8l{WMPW>X_pt65P0bsH3gywbDMJ"
    "aHZpcydzx~bhGGV>qr0dA9N|v^~H`JGP%!8pTMkPq81kgl21tg=RKH@F;_!vAx2Z(Efv1h%Tdvrt_8YHIdfnm{p-K}Z&3%TLaqXh"
    "Ry@Z`Xy>4Pkp#-_MNcy(sxr;RDVEb%Z(-fX##@dTbektjoU9}RViHOc#43gBE&2<MgjLFyEv=VP)>cg?agT;#AGy68wGPjcDz|Hp"
    "Q}`)S-k!-JL(k`Rp6xcf1@=lDWjLBh8$}{}1BpP!Gj#(yD~ZCvTE?*1k3oAGyEU9#Rq-mt$oe-mT}$1ueMS$X@>1$EkyoF6J($9D"
    "lYHi~JXu!c{TmrV#Oc~nP3qi%>j~~@xMzfvQ|M!1jRjY_->6`BhutIg(m2(g;)=5gE;i`Q&@BiVNt>t!i|@NK>&hIfai|Uh<@MD)"
    "RAEGYUs!FsXPi%bAooUr5zFoLIF*&`R;ww=9rGnG8hlys)8TL&H&Uqng>P8=hCMh~a8H5X0RI9{6JAqz-5_$YR>7f%qb_bIc;Dj7"
    "!<XLw8md)R{i`}mDV?CqlO|2NXnR_7X2G2YcRoC{_?+V)e$#LMQ#L2rADtxhJs4|$d6t5bWfS`+93;`KpfwUY)Z&SU6nZaFT>3aF"
    "wN%-SD(uxLp~i1@EU9QmsXR4%vP5<UIT{<@=sw_9Nxw_{avs_qbPmwXK`(@ULPUV3H{p?IuR=d-29g8PUEyGklRj<}cpJ#$7l!hx"
    "l*m%zA@Qw#gV7_?B`Yt4{7HtEL%DI(YscNw?Icmjt3^?j-Whri=rfShmgJ6;HXA)29Qo1a$AF(3X_sDL@dxDZqKmPH<PD5`T<-E9"
    "JhaUL{EcLP6lPd)uscDkgX0d)=5Y>)iJLs$3UR`3Ue&9rUW>_T8PzdWSMPCSWlhv{BjqSoj#}5$+Em-8R0w-nl<rYJl%(K!kG26h"
    "CGy5}Y0`B}w}MDCg$IgsRHf10NRrNLr<loB;EoP3Zai4<;3BCa=Nn&h?C<zhi#sAFl$3;#peX&xZ-hIxx8SmZ>s<E-o(Xs-;eCbo"
    "4HE_yKUj9KvcTE^YZEj+*coHzD&(@$LKRM`tat8#FztGIHCU-pQjIJ%o=SgCBW2gA<<s^>hbo<l<Rr;iN>A<+@=u&@a^}RvHCKk*"
    "^0;lYU6+(&@1)x*_wjK>#dii0OGvyRzlXvds&<@RvKL{9)<KAiqXbOSFuB6y0m~F@G$@@3mC;#C{>bA7{s4Rno)d`rx?Aw+!N-;?"
    "lHx`*AcUTq$I4JNFlrBMp0Md)+mq^6hJ(GWC~s-Q8w4T@F}~n5fzu++YB)D=S;gf_4Dw*&X^y9pq@1{QbQ^Lj<qgr(an~3_y(|Wj"
    "W70EJZ=uYqnyl1x^*<CJifYwUo1K(L+z!-ssP-*&9jMzw-2(Lp)zeUcue2dWwj`t^E5iRw%_%*iOp0;~%3mmdrP7E>4`E=MC$jpq"
    "h-fpSjYIp&Pb!LbeV095Gj!e3HKO}FE^gSR$C>;i`3^m&6f`Ml$NAtTif$BX@aaj3!>l7@#P*N}6CQd}0#nE7`@ZB^Up-kk{zGnJ"
    "|0^yp{Pj=Y(35aHH;36!CdPH)GZkw6q$E7S=%vazWoDK2R-3BY+QJjOrId~uQy<D6sF0$9L(K&Z7c`D&;?XQevj_P%p$%5w+~{*_"
    "!bTvklvwTYeb<H9nrlLH3(Y;WTH?Ms8L6YXcEHxwFb01p!d?9r4n`7<UAB1jl#y1$vl<4<+^P9Yt+eU3)HxBO|CG8O<CDOJ3X7EL"
    "O8}70P+>xa3sr4lc^>NIR4DW*x(XpLapr7|cN5+(HRjgMlgR7-^+Pdf9e=$J-zfi$>e5~vbCq_n-#dfZ6j8GO&Y~&nfT$;4_8-|Y"
    "FC4c;qik}<$pfc*T-msp;%<e9O{`j`dow`Y#po*8GKsZHJyhCMSzBsYRQ9SqRb5y0(`vj^lcAa(q_2mkIt1#}RJO02qH=q2I!&lP"
    "E9qjnM&%7vl2kcUEq3I+heV-ff=rX9WpT6Hkai>5XX)URW0K?3`AD8adan<%dSK#F(Hq=bF1SivE?>B*ZE4PJpZlR?-E11X3ppsr"
    "X#LY4SkkL<q_+`1MDb7l`422damJKJiO2Xya_`Xo>YXojfsRH$32;-w)qF&9_2pHBrD@dEv@1MDZTpviZbJ&^zaPPKQ?8@d#_wlI"
    "UaAcX{l^j1xX!_P1kX7H|8G&(mZT^U1pntqd!xO=e90)Nh%dzFFzhI!sDN)&6r9UUS9RZFt5_rUzoUVOs;<l+v$Fz505t<@Rj@sn"
    "u3)LLKgT>{gUTadL2Gj)Tc-4N>3{HMzki93PvO0ZnG59;i$=d%5Lnp1;Q55t24*FsIYjN+p#xo)$%sXvR!3RBYE6_gFDwuNtJ&5)"
    "-jGA%4N)CDRiT$7+y?lb{uhJNv<NG5XDENg>sd?|75>d07k}S{#u%D)XjP#%gF(n9pQ*)zJ24^g;QY<A`$pqi;|Z>1__oH%mapqD"
    "kJy><tX<HNH0An8kDP8u52b>jQs{efoADiCSEo2T=$NqBN7dh|Bz$@0yqE@~e?2X7Zucyv>K=r>6hlu0brF~%_!e<T!~+ozMeK_B"
    "D3TB~pKu(^dJs+FIDrcho+kgWy!RlBuui!0!4S`#qr1Q$#wf&n&JnUF2Tvhhs+eiA<jCq;S~Y1MWZ=r>Lxvl<8_B~#9*O^yWKxmm"
    "E)x$LxsR5z#>&~NFjiYf9qtSdAJv!Ff1>D?C=rm4#AYZS9+d7-_QCVO@553@!^J_2hnx}irH$7GUiWy-@s<$amiky;*E0Q5T}?%V"
    "w%H&wZ$rEJGy=`{(!3XENGg+^Jp1Y`edekWC|TXqVfXUNMj(0zT{I2Rw3&ht{|Y*MUB$`fjR=MZG3UWuBCKVc`^M3Ve=w=wy;7to"
    "rgBBCsdJQDdg4ZGmx-sSzDF%c4=AiY8*t`$d&4Yw_RGyx-CWtcfC}3#iP1@X8qf`R?KlFL#$m)(pM1>*8LEfCRl1{$x_T#U&Ba@G"
    "#F)AvWUR3>p^<<+=J?fRj(L?YbkGvDx7CqAej#9P{^XsX+!fk{>6kra7wISxAM~-FxGcmiU^-%j{05^XQ;;u{%aj<#e8MCn-pTS<"
    "_6Xu_ls;2NgFkbW{RKf$h6V"
)

# Compressed UTF-8 toned ppinyin ("initial final" -> chars) table aligned with Sherpa-ONNX WenetSpeech KWS tokens.txt
_PPINYIN_TABLE_B85 = (
    "c-lOC*OKczlIDFqML$Qq&Fs!>&%ZnS_f_A^QiigW7iB2#MTxScB<f;kqF<!ay~P4l^+cSAN(T@G5}98D)cKchXym{C$6x;C8!8L+"
    "_uu~bhH4AyDNX<R*KaI874^sebN>AP3Exm0K)DUm9GuedjKh0}`6M3C^enTP;(eaOC5I~xHyrLcbR}MtjPdE<Z{I)fiyT5Z)g!_3"
    "8(tsP>Kn2%$ZptQY5qUse|}T?PR@?DA>EerY;wQDq0eC;iJT<zGT6x}|FQj>>Sm4A7xruDWN_J`_?Fr`Hrmo?O5-FGLne8dRAthU"
    "Nmo`$S#4zP$YvOIQ2R#1lv7il*@(BlslC?JKT3aoLm|XmoW?U+8(i^unrH9I=j}(Y9=*!*XvveK6s}691jU~E`~RG8==Wi=qWFNa"
    "cY2NKb>mov=NakUWuF%>EnXqA{No$V9-4h@v}oDl=8n&^NTDYDd4~J|ih0cK(JY{UrrLxq20hd~&p$?g{f0ylCl*c8Y>l{E;BAW!"
    "Qz_-8QH@O0pqwM-XP7@?_e@!XyBj|Ge7#7{l+980Rq=}2bo7mRHIzCRGF<NS&6Hg-lK=5domWKt?VFn^v3<=fm8-;xkm$fP13McN"
    "O-ydk$Wy9AW|qtsRXiGZ>11)Tz_l9NFCK4r+F`HH=RQA`pJXL*l}aeqNCt5koTGl#y!Q)ukI{Dd;guVnE>thGKj-&d%T`K8^_?cS"
    "-%zVV>k54fCUIDeVcUdz9Q+9c83<1B%)|2u?=+_Jm?~kTkIf{y4Ro96cJR@{_kheZSqo~!s4=FNPpyEu4H_?K*QI@$_6E6ga!;H|"
    "uo-f(#}$KXO|HMV+2hHQX9Hdw*{$#?&gVMc?(8=>NJ;!HHB*`yY38Ikl-5o<wrpb2u>btVY8GoQ?nk(H*{I5<AlsU3#~<}yzklxY"
    "pVtKjYw)k&->8+QO<yZ_->|)M@h;DYX1#A*x47NX^y&L2IQ=|bFq2U1LbnBX8!KhJPbvK-)28W}79-l%In`t%&)F;2>wKR`$rj6("
    "T0$0aS?pzX5<jjx^wltmKd&rQW?;2p*n*)0_aw%eSZv{LhPxHj-gJ0#JtoPeSY6#J_!dL2&VR<gA-#d}4!j<mGH^R%&cb4Wnp4^u"
    "<W)Jp<6~bkc4VwJL+OCEcEqR&%AK&X#b%YtJ8F-qQ{?Pc981RWkA?F7`yTuZ_!}G>c<D)E9%+=*n?4lMup8sRr?kh(0h<G!S`wzU"
    "MNK(xv5E-~`!3l@8ds&0kc%sRDN-vRoYm18Qa;0HPwMtZsvg*$z<EWN2|aFFW~+`0t%y<s?ro{jWz+re%6jOLuQ$<uZoc7|lGrn%"
    "Rqu%f>_d2|w=3Y-$HfrWCwwgNZQ=V!u?CgK<iF@S<>QG@HAy(qsDB!(ZqpYqHeej^a1+y0;{PgsL#_|G88j|%t)aO@YlhYiPh%=Q"
    "$hpwi;IhNxgXG3yU1VC7ncma?wfV-*NMfc~dT0Hkq=G<RELZy1i2dXH=XJp14yy*%0vwjnQ40}9I=xX7*nd+l%h{!5t71*0Wk`E1"
    "yY0t9ZQXf~u5o%)*@(#|8;Nx;=z}u?XAJv3xF>M0Ts3%FlgUeFM>%KYt~dWbD#|j1LIWxzm`~vK#9<8GIxQNst<z4qP?EDdNp&LQ"
    "Xw!7CYGHeTZ8h2yPSTV((L6?r94&{EILpA0X<DX^%=BXX!}_NEj1G6c={5dGUHQcj)SDI?Jeu*O_w_&j@^>ALDZ8X~k2WcCM|7|v"
    "@m9s?9>mhnsKEPxi8Hz<ys7*XC(k6$r?)|Ghf@X{7p^3uHr_Xs`dHXtX)Og;>N%-*BE{(&E-tmEG~9|GYEiTYtN@P&`){fG(pW`e"
    "<x7bzoa<O!;QXjXYWcBJLu*gf6HQF^b6Toq51&u6Vi99~b84U{ot^AtMEvp1<sR2Z5u;Xfcf?Xa@i%4WWG%__B83Y2iL$0tBEVb`"
    "`xOf6Jc+Y6;`2CCsBtEDnC@V@i+dCIZSFSMxqfti{pQxEz1o)TEZRQW0WMznY;dm2gRRyr6~0(N{0v4Oi~^JzP;%fA$M^%254@Oo"
    "-_oubS?Uh`ohDam%w8486TH?xyxJSb1RSSmhqQ@?uh%GmY=GMlpL%?$XyNV~(^;zBXcDKHL#HAavRqj5YQS!h!vTjou?ymNwXyg2"
    "Z^%^9&vV-0=ZVAUWloW^**}jGl*bU(AROSWLzTT&s;K!+QlU?W4A+NzU$Ea{|19aT7-nRkMqV+&y=x};hA@HqL`3UlXrp;ijE<h&"
    "8rJ3!tpZKlqe3rYlvOza<s>u@GOftE`H`w2J!(JC1;ldbE%1@z?2&UFHj8XMBa^><W35Oh?Q)-Z)+wO<(wy?Dsz=QW-L-T7S5Yeq"
    "2G~c89dR(gWtxm0S#ge!IKJlifz3Eqk6fS0T6=+tB;>Uuh)1;Yh<=s2XGts-ufhsVrxBrEqhZ2pg{2Yow{*SmeI=Ppq|<J+#B`fh"
    "8F{QDks5Et;M|>aJ{PrDYp32#@uZjLA9XbkZ}sl~{+EBzUNNj_^6%QQq0xi(mW?&v{a>L%RCiA@3$e=ol&DwC+K{_o%)r$7BOPhv"
    "<v`+u(-&t)oLzC&mw4)*TGhrMLy)Ij>?2a?c#r@Umx@=M9B^8~ML`N}DRrejm5%qZQdv6Lq3eqt2M(2wT_wGd*IL4Sq*rT=+XwF2"
    "znt1?<q1~IUsk>6&4yba4_#Wl|0z__wOXNEgPMD4k7!+^b?c*3i&^n0@#$MfhwO>(^@xk&cC*b{`=9JzzopfbmaBOc9e%<#EPU|l"
    "aNFZC&#Mi)i;qqr!S;+h?+>RZzl@oH!lw)qGTO*^`m0fs6dOTZ`A!-W7woDm)TKsRw!aTiF(nqQn?jx74&l<o$_s}xT$FIVp@L82"
    "GZ(g8xJrNXu~V5STtljX!!sX)k4%kVb%Cu3w;Vidd<7U@DORL;n&xW?$`n}ibhw}6;g*LX?=3#GIH*hYD2<UcE;2WNdr~8gzace*"
    "b_=={7<sUn!=VrNJA^$>IGnz-S@;<1IOf4^47Uk(;@IosYaeM;QPY3IaSyKtgg%xgXiYK9^4s~Sm4nYSJlgWi<$aS6SH8yORhKaK"
    "t5>jg@tH#%+eR1I-{H`P_Ypfw^m3e;@S(urK{{FKoPG^;AR7GmtslP))hrB4Fl@l6h{+XZJj_-w_r}f;dm#>%xawe-qNPc@El*0k"
    "cG&GovLR_tYE5xVQJ)G_Cjv0jP(DKA0k0D_8rYms;zdSCZJXX{P7nDqmB~(KTe(_MGqn@?4;aV5d%)AiLL7HHJUZmAxlrJ)a={_L"
    "hZ0Xq+>`l9R)?sWr#&#Al-lQoRJ`TPE+Tc1g>!~G8y<Qe$=_9o%ly3OkgCJ|h)K0^<J34)Geb*<c0L^obXIX?OpiP7OT4e}y}&_U"
    "(p#}V%b=ojQ2cpcVVc6&1&te4Dmc7RqesIQU1IboaHqkGIWO+goXevZHB(_aTZGaQ9$j1=@e$|zgL_5x4;*A9R~B<B^^|m`Vwc6X"
    "KW)@1)-!lJ(Ya26%c%*Mo?JVNk&(hct~v3m+E9o3at?D27T#F4u%DIsUFMmOP8W%WWG-U0J_6-Ol^E7LJnQki&5I0&?_Z5_hUW~H"
    "Piz-(+Mz;|%S~zL#a)PZ`q*f6Jcs8JV|mP{DCP1sE!nzQgGgWdCa1;rlU1Ghntw#<HClDdKd55Uz?9_qSFCqU9IXmkRUE!>oukT>"
    "1`aI_v^=t1=HZnuYrbqFds|sIG3S05g{j89Us5?hY#__HtfT!H#jhmP7SP;4YYOW-R&8pXsWYP9g|-8_1l(QmU?;n(oQHBr$^9sg"
    "DE@y^S2_)b2k#LY3-p>)-q1V7*#zf?TnKp7|5*NA!QB_Nqlr-o3^EET^udeavL&y{N2(^fJ;Ujn`!6{dTBgz&k5<y~K8#8j!2*mp"
    "WVd*!P<>4G30oQ7B>0e$@=$7?v_{fad3y5OLJhE#fYJea0XS!PIGF5XLxI*S9@1j^A2YR3l?x@J&84Amn1ylx)e;;6jH}Yi<=BI#"
    "Dven3iq`)hmhO4)fP49{YPa00*niL<Ek-oi8sR3Ekn$ii$NmLpU7YP{6r*X0r`?ampWp0^zy4L93JqNK_usy$n^J#2=RmI{^fxfr"
    "z&H;x2Z9zR=eU34Er#KdN;j%ssMDjvP^`DKUt&L`Z+}`V;QvfPMOEZ=Eahme;?J88Ny$vas!44iwM}HG;%Lo`sFm6m4m3>|Ou-po"
    "*+Y9oIg4roYMiK<r}2<uCikzr>G45r#u&eMa>$DJ((ON)GYz*ImJ+zg;6j!B8LHo@v!RYBBU{GK$52gn#)NDf%1?N0@R+4>jt({U"
    "5@I)GpA**>?<SW+WL{Gn!lSIuw}R%C@BG|{KlicJW}1(ZZ2Ar}wFxJbQ0c7A_q{v@AEkQMQMZdw2{w1wyy5hW`wOMhbZF9TK+lpC"
    "ZK*#ZD;1B^c}N!^T?N+%H-tkGV^1{tG_FZwF72Uo^O32lFLH5k8Zh2t@_@rQXD@sUBAqHRrUA?pCOS&xF13=DM(1rCHzL&za12dF"
    "_2dw&6x0luc42B`DZ>-}?h$~X;VvRAwNPCc*f6|cPyM^nv&g}b15c@Sr7>MZgi79z2@F(n+JSe3`yB2r+^-O(G3H=ay}CiA6V*HP"
    "sPNci@6P8Nzn9WCWF3=-9rdD}&wxe~o<2NP^_<1z9*r&<0k-CN*yC-4p-;^sSITUg+^h1s!S3MGSnZ6=69z}vH*hq;bsY~r*=urQ"
    "TpV$E&8ocm*UyPr%z~_{=bktnQL0ULi|igPJz72}uq9s8b_%%EESP&3v(c+jVn`Vknbx$cbFss>D~BP!yArQUDJT7>45~6rMhz9F"
    "E$aD>@IY?%q4elZaP8wJOM^8{6mqUfY9W=5H1wv>J0WaAc!KbTiIdd(TBxd<_Y`^uSWe*HhvyJmC!9>EpP{iw+b!K3de+%WaJ|8s"
    "IUj6^SrV&BJem}J%mj@q?7KA1aD2?^E-w?jA4|cJno4IXyHB;D%IxhJY)059VB><z7+$CND3F<=txd->y@y<U@chk-0>9T%uF7UC"
    "TOBl_^!}rUo-Bg#fJ_ncOE4QSD8R|W_y!#lCqq0BXjI|kl2vZ`*9Zh)vxVa|wc50v(<Z~I0~b$xG$huNbX!_&*+z5cf5TiY#1;Y-"
    "(0X_uaiYulGCK=)d?^;C5sS>!j<lyZbI~i{Yeb1AWpbqIM5itBxWrw_7RBm|Q<KqKHweCQIz-pPtLn5wxgzD-l+ROsOy-t~OB#=O"
    "z2kk1-G^+d^3d+16M4&mj)RpQwO1UwaWT!)QKZs`>jqTo5u?iyA5tb18&GURbB|^kYjsp>(N0q@OZzp)UK~$K@-8Xw(?%C;KD=sF"
    "K2YOIyB-g3JTZ9YNF|V(9a$*{&c-0C>cb_xb9iWLsR~%_ZbU?#)G6*#CQF?M9p>zBwMM65%+6?+)0~QEuM#voaHe>u;Nwh_kixzc"
    "bJFNX1`+U#|2&dV-czYW%{tjCC0!()mrPqM-A$B?f9^dnwqSL^dtv{GhY_16*|+4Hk?Th88+kS&TUBYNd*nV@fuE!f;5sL_ht!`)"
    "t0?VEB+;ew5;}JnuP~WG>x_*#P1Ee_InoDMxmrZ1*-lJw=SyrK@j6XkF<-#aotk$}8f?7y()j2!V(4OFgf){7jy#r;Sk0vSghL5D"
    "8%|fans6;(QPpKFtW9Wkqxq42g~aFDy7CP{1M?RgjwyF0XU2;@yQU=Wvc8IYiflB}aa65zm8XurQNqSdf~ym5Y<N?MWNI4f1I#yZ"
    "P~+}eI@)`5-gYk3_i39Xcgph>pLTqS8mNR#w$Io5*Z<N%m&z4V-mqEAW*<pZm>FO25aO|fC!6vvHL|q5kelK{lD!q5S2A&Bw)<GC"
    "5!FLYj=q2V^3SsZgAqA>+OB9<rdO9<2Wifv9gA$#a%4u38RIff$vLH0l&O>D&@iOwmi#!YUEuWCccqdM%aYsf(@f3ye2LjNC)U#T"
    "WOo)Xp*8wmA=iM4LKZ_%zV#Sme~;dsOp97a>SQ?)a6V-3!Dn9@DRD<pGqt>r7sNxDnK0YK`;Dn78dHj0Xiy_>O{akz`{F%6*2*j2"
    "cgRd(mZSTIo_#r;v}UEi_N~u}8K-LzuTMFLFh1d+fRiU4Lwp;Q>T-7^yNf)>A0zcPhAXkL%%dqkslk+S{Lr*2Hy&+BC_K7^oC{V3"
    "rbU=%U^NG~0O5+VSGwEu>~h-Y{+`Euo@RJj=efblB=0BeIeeP)SruF+Upjm_OS>oSzI0MDpULVjdrJ<Mc&3~T{lq}Qtn$yx{&}|{"
    "vw=bdc3rr=!B2xffM*L{Z;Wj)cSSRSy&?`uI6C9JiU$>kt9aStbxn;NIV+mgXkMo6oc1a;C+K0)tIfF>=gyq(aDKw(g9|Q~R$Q)g"
    "cgejR_Z=P|`T5|mB1u~|XW6G@@5yN)4?Q7Kty85=K%HmAsu}Ji;ckFG!j?^qH63f@cj*1#Mv_~rkC8r(>Owz{O&<p)=TDLtNg)tx"
    "BWF{a=rjGs2<A!5<>+S8!{xHay{`1i(jUmgmWe7cs^T3ZYxUHu0hJX_9(b_nY*TRK{FX<qq`P7tK1Mot9pDg$lMAN<4mLQep*KNq"
    "Ny!&AU22=8yt`WEUV@JmNgHBXQa5G1lu1eV5#121F>%CZ1$$+jFUalETjdU)-B)Cxwy&4_Q0gt~Yu?;=uT?tyRZW;~V0XdB4O=-<"
    "9+8-`(dBEvx0a-OlFdl4l0a{M9sN#?h|t?%59K1}8vL$*MEZE}4Y3Kfb95e25O8+G%Qf%cvYBWT9eFBqdR{eCS35+xkPGcQ1U<}m"
    "(afW@!R7*|OMH~6zTtF*(^XaoBTP%CCz+8r8+kmx{;HwvHx%d4SYaW7C7WIwo+j9BespS5rdy1yKb$`Jb1^-^(i5*6e5EN~pqc_+"
    "Gnzf=Rbg+#XIH8hx!dBOejDkc(T-?^kFzhxzM*i$x&pmD93^nHz;zEdNlMh&JM&q=;I*83Q3LOr-x-OQB1XYt+Xedty%_XNxIe*f"
    "ak|Y$gF9+Nx3!UW+Uy=x6s+|)ZgH|G<F(xVk50i`zlo*uhf|&vQ&9Z`h6DRHP6BH8*-G*J#ETd&64D$?t0#w<oXWo~bo$AGHNjB|"
    "&nh<^Be_b!K@1XVR6Ce&;FbX8iw^@{7A_vRyHj~h)dN|5>Sk$_=JA{7WnQEt@sj3JT7hh@vfqhUm9Qt_P<N`4eYFD(7lvJM`Y<lR"
    "dBFXwBzICYrJmMC%8wvd0To#zMxQ@aaB%)`%3CWg?MIw#b7>&crOb_AjXoQFL!g58gz~pYsIp>V4ud}IQxKTgxl!j%2a{Jjc1Qdu"
    "@M|gC$YE45frB1S<G5H-p+?s$muuV$dE(1FsVuZd9kvmnCj4Ic@yD2*;#?JWD-3e@tdia2nkiN%GE(bc#liQ$_px=r;{|VXItTQK"
    "@n!O{(16FlMU8fkNT4h3H&pYyp0Rt7WKZh)-89(mT;b3YD`J=G@oR{gP)X6KK>G}Lo>Gg3rt?tH{s}r<us^(dhOMVgBluPyMl&oF"
    "u<}OdiHkHFS+3gB$Vqb|&8f8G(ydE>CF`eby{MIXjhgn82uMTv$ljRGqe!5(>tGDiAxs@uOku5Rq8L^T9Hekz^4#S`nO9qOhkSbS"
    "c_(r0%Nj5zk6;Jj6yUtaMjFRu%Dt)el)_%>W3e+bTF59AFB-Q(&NwM*D)^SGo+z~&ex5I=-k@E=);^M`9j)^0#pRX9?u;*K>C_{I"
    "o{%{7S8zDP;f8$!hgCe)snno)o0=yY-f5#?K%dQ<q${5`I-h32*h97pIUBqY_8pvzX!xY-kphpNcX}zLZ%bw;=2NV3)J~OFi#l~p"
    ">Q<;9lG~wEgTgu+W!bc4qy0i5;h)C{avoSN)Mn6IV>XMGIM$B1D^RmT!Hkn99zW$=k@H*buc(!p1V@$o!w;jz7j&tSr_kqEgD*8L"
    "w$)0#p>^QwJ!1d(rpufj(Nk5B=iL57sm8Sz3==qR(Zk``TRcZ;K3F-lA-#qC5cV6`@8LFsXAR>!tUow$<nDwQGY+;AcB2-5S17#?"
    "W{BPpgFd-^dR8S{lH63Rl?=28>9dLF0jfu^8nCFsZi>SNDkOfgXkX-VhRX-GQ@rT%>&Pllq^EM8$X&-~9S9DMh*0bMHnHwUjGBzp"
    "5OV{p#jr8P(HmDO40F_VX<^VZ%a<!(8*-?Btkny9EjYLE?nVN&OqBulJzT_T`KJ4To<nZMrJ+~<bBJ|Y!6QWTh|Up@11fi9P>z(!"
    "=T-h4C_LDr?U7BrAQ~K(r_ff}wTbf(-2@(2lyj+jqrr<-F?tOtY;t16nF8B+c8<J?vzy?<jb91L-o!e~dMSQN{I)!6QJ=a(Q;DvN"
    "kB}w`{&+l6=xo4YUfh^?i?4tGWVx&b`6LvYuua3+#DRmOIW7aLKd7;x#)Vf$cKcudyqU7D&@aOz50k;y@5L0<r_k&|(}r0BW);}a"
    "!GEBtUW#KZ+gPjMWrEiU-aGibVmP5xgVHl<t*Kq3zQM<_#CGCl#9Mv+^JWVlYB@enRne<h!bg<~>O~ALyS$4_Z6WLQN38}G<}nt-"
    "Oa(JHTvYJ3=lgG4HM(98wl=s29M$mbV|b>LOV)v&C$3Z_nUTs>>Y;3h;_tuy`<5(ofaVm!B_^-v5Aijobb+iMbz;=bkjjc~oDK`_"
    "w|F>{cvZSZIppM&kw;P<si+@)dE1483&kAN8qn;(OM%x8#=985VA8?#0y}f;C2=*tV+JopO0H<3R(wpC3;An$wCQPbambAhH?urk"
    "^LojfBkxb_dVEUp`Tg~KiDwVs@xZgHiOy`-`F;NS+}lCd#bgz;O?<DY=#i77*`4M^T5Y&=<JOy<CVNXhFQl56##Va8sO9M!&yTOq"
    "-5%6h&|bl?iBp?KXFA0=f0V-I>-TmKw`1JZa5ti2D67@if8Xz=3!JEN(fazm)w9kJzA!$<!VJrM>@=~PqxgZ!9=*HV33zO<+u>)Q"
    "!<=+a5`<r$8$ifcpwhv5j$;lV<5FLL{ayyrZAHrC|NXx+cmd-@>?E<1;%%M}rASrz#zY<67`pL@x75;fU^>I+jp7@zwqKtM!k=IU"
    "E#<){Jj`XWlXd3nb61FoA{rqUi)b~`dSXlM>l_|i_#9KLOQ}4)4_q2@Sz*YVjO(&^eEr@fqGx=4ZVjP)gPMcwJa!g%S>dgWk1WN@"
    ")U8o>Kq}qETHF~*X)ELLS9HC|_Vnv>e+au3zuS^X$@>2FdwrOBL1hE3g`E~&Y`k75Rg*+Z60QtuGF*NA^Kx)-!v2oRE4q~E(c<h{"
    "oU)9QU%&T(tr>JnFlnH>!M8)l40ro{T(iRUpL@Ycm8TU}>wXUSvXSdq{AgVI3^2a@`n~-F=c&n+is}u>hfv9aRY3cO!wfoi^iFtH"
    "fp1K9j_e}Y7R^KQcJ$tJ@y6vccQ2AmN;@I#rVN%cF39XI*OB;538VQ?Xo6aeuitw^SnM%wVBEq{9G7cU9^Fp!Xe5_t!ZqOFWr6n#"
    "*+xUufFss{QWq*E7&O3fAS_5MEuMCg&vo6t49_;6RHRtbrb_2Do!b<QDcI9H$;BNv9^CQ8tVpvYt&{AAa(>ADEsvZ$3SYl>e4z`?"
    "92L&g&Qo87iUO&?unxX{ukS1{j$oX@yTG%Fu?}Xhn7v{4j@cLHvY4Nux5meoY7WhMv@P@QCMie!W7LD1^V<zoy|)>&8@*Ye_WuSZ"
    "SD4&j62NnTO&e!DTzKd=DSaZdLWMC^yL8Ia-Qnbjs~hg0d2te>BGavGi*iv9h~_rXCoUtrRPi3lJSDrt*XNF}mX+H2>vN|Et>xG6"
    "WuCF*?p4wg8QtX>e*IqL!PNcwy|3#UY`wXu<F!7?t6relht`&tS9X(;Q1#bVb6Ay!_4Ru-_Y*9kru|8SkDu2K!YrB2*FSH_DlaT&"
    "DcR=cl9w5NO!zhA*Wv4PFD5U&&oy@NOHh46{SEIICcBt*@!?Z4#>SaDMSk7BK9^*$>Y_@(lR6itT)jzKN5QCexZF7Di822AT$VvI"
    "Pp^iwyI;R|T(g%Xb+Y*+{#xqm-+wF7C*}3<b#||B@nYfQPL&yVO5z^B{`X)0_?N$Z%Vnx5{n^6f21y?_aU5J}`IJRNR^dmdYvC)z"
    "XOP@Mt_ryc<c^RpLS7~AI*fgI1X#PGql)_$x-Yyg@Ls_njn6xVEqn)*+tYqWexDWm&K@{7=VFDMbDo>Li1TK{hccf^e46t4$hQmM"
    "D|`<m^OW3KiWjL?rD{oiFVlg{+}}az0}TgWc|3P1Kcv%=E+=}}l0J$t6=N%%;>S{ly$y7VaGGMHgDR!1o7{5vndDcAUvCbRlD>$s"
    "kIY9Zp7b$mV|KvBE|<n^A9;L}WLeCz)LP<f#W~10A6ctP=3zo?2u=V;1CDv{8kn`H<WNI_;Th*=++FZ`#O_Ac(X8t_CN_pd6|zsr"
    "Zy^7K;tEy`*r_DBMstrvh4RPPS9l?YqduPF_^MNXL*of;cI3wBQlU$a9yJPG&L?<v;Qd_2Iq|IM0lGk$Lumz82@XeWRI#<d;R=T#"
    "jvCbLa^}g|G-va2^TgliHo78togguWyaVkVOnsP!u)pHqhL<E>d{VyNO3?O1r#2T-T;1_F%eOMW=hCQsy3luaPcYulSEX?pW_MW3"
    ";n2jaiP<_<Y+P<|_r$%533W=BD6c}WsyQdL4QOxDO<|1`Jtv%4aZWkOk;`eee5ov?S(DaT23r}pa`45Qh(Gy#rgDSi1r|Lx7;wno"
    ">W%vy9*1}_@cE{uLDL#N4_r8Lv&zSeoN}Lre^dK=3dt=jCa}!Fwtz+vrxso^7#ftCQ^ureoRe2J9L~MCpdxRM?I90$e49!#FO`z4"
    "8nRBwej}bOx4HQ1sBaZvnptQKu{6VyhvgmmeQGwTYtZ7(<t2|=(!NT!6j`gPM<K@bfEUp#eS(XTz+93p=}4k*OC!Yc8kaV$u5=3M"
    "6jI>u<s|KtY|FCU%V{NdM_cQXBc1_oi_JdHOH}u$-Qc9nr!1dCvBHm$+SBhH#C%BQAXA3i8tgv2(wN<%sc`cM%P}k`(R$-R`OX$6"
    "F`V{rJ;u9D{VENfG`i9FNZSVO7PODiVMM;lq;oDTcrxQ{h7TLQKH1Mmy)6Fy_p$2TW=)taVYbE55*H^bmN>Rz%jEimw>Iy+kEL3l"
    "*YhV-L1zZJH-t|tjIokN%g6c}8yoaDWR9p?qVAlA8ydQF8IXUXph6E-5S=;I<@AY%JziK+JIFLCvmoj_ijzhe>;)y#lyE6|q;;F$"
    "GZ{xqqL1$U^p9G!_?=2kNn0}9$xz3KPq`7BL;eD{2afV&-l%sYwLu<Z3KNn_N%tkT8QJQcUWY^jj1y$*kWWBq2Av~xpRlXJ*~WYh"
    "OKq%gaCF0!jmH2_Tgvt+d!b^BN^>fgsd}bikuD9oT<Ov0#**h<o(H_h@uJDg3GYhm#`xf|f0f2q7D+iL<ebzai1y@ih{XzCYwUPZ"
    "x=W)Vm#X+5y>it~8!hc;pD){rH>fImk27lBR!STd5haLc!H7XF0l6vUt5EB~pa}CC>?YtQz)iuc0<S8(3z)cLvV{2<w#L|9;<<#E"
    "65g};%;I}UvmGsObdJ%rL%vD=lLA#`Ea<V~{DF%O*M{88b5p%vo>d{dZ1aA^`vc!z{Pv}jmaZ+mxb$AKn#p=5n}-~(a#RInSG-L$"
    "VD%b#56n1prZ8#2yajF(D`V{Buw&xoj)AIkwv>5Nr9|^M-E;KpaB;<Rj~5j_SNLM^yUK4{VxII*pH6j27K2O{#z|N$;JJn{1z{B%"
    "Dz&EYwxrmAibZPWsMVp(fHp(gTsgVnl+Evx#2TWYd7~%&n)I8}??}HZ{hsc7r$W*2=V|`Du24KdIS-W_Y;xcgFgC;N7zc5jv~i~L"
    ">jn49O+0Fb)OM&FqivRsMN%m+Sa2%OR=|ybTRC>yd~dQ};it=CRcu49+WDhTYmX3HLhK6Z8{`&{&w#my@(f%Dj&r!Ff^&hHCz=+T"
    "4i@)V>r!G%d6O1fTJLCm<#?CP36E>MzwoWk_kqM;VqC<$i`A35E90w7YO=3KPt>6%52-ku9#~Ylq>Jw@l>=(Hw3*O;D9Mp5-m)%h"
    ">p)@G8yr>QtK&3_t2U)tT+Z|DDVL5i(1*JB!jHd#%mZ=*FegxcfwhDB0@?@Yg)j_Y{Dj2@7F#%0K*e(_iM2fT0vx1qHN<y}@+m4+"
    "$(m5(PK`Hp)-=4*#HIO?7A`GS0bHbQiuMh<_30kcGe=>Z!VHC3P82x5<XMb&W!}&DQsmo+Zx8l|Vh?2K$z&|Es?26`Ez5l*uadl~"
    "(a1H9%EK+h<`mB}8un;s^61U8p=7F(c}uhpKNrKn_9r+%dW^|DwhGv}qKf%_o4gZwAzcb|t<j^*=`0sYTr{|t=Tei)aanc6U5clG"
    "utpm?cO280Povq!(iW!`JSEBOP^C-F7dZ}_XD<8Pvw0HoH6``B*d-Y_GT4ZUn=3!+MC~ZUhKddS430wTdepsB*XL4)%LRTEB|8vn"
    "AwfohhH9Wstuqc>&Tu<|A7XZhg(;RwSaZ0ummm?@=;=uRggZ#4A=QRr08R!rQ#icfQN_iZsv+4UIxpy!=kkWPJ|C7+m`i;wvz*MU"
    "pI-D8)AJ{2K+1sZ4vj5#T<ku`8q(^;i6`&o(msmQ73VJFtZt;@`#l6JhJ_B+7C6u0yp7v5dV3nKXgA|*j!PC>L!RCE{*u>F!sOTg"
    "`ODwcJRC-vP=FcL#3x<8EkC^ezIFwvEYv#Cn?t_^qddG-j&iBErrnKGMK(f7R^;e>T0~js0zDsFH?$o*#i@6t;hUT{P3Lqm>C&WN"
    "#pkOe`jQGHca*9fwbdmxnF(H3**(c+7zwuu5bvQm#MB&93k;I@I?%u%w@QZz$5$M`u$7aUDf4X9V5e}3!g+O=m9T7Jw?O?DS1;T@"
    "NbDlrmdsnSi$|v3H#};XTcJ5ZbBnbzzOS^)vhw&=UfOkSqzkm^o!6NUqvmS=0@nt2D;x7yra}7%Y84zAw5!lAV7C^j^j%FEyf-HL"
    "WY=juqjkWYh1MxV^AqO4y1-!n;s5w%%l(l0>?g!sjMm|=Ko@?`h^~C|ZL6s0*zg@<DvTIa!uL!X9B4A)QRySnJ8t~*%wfJoiH2C~"
    "Nc^{N$}8+pk-lECXx1^OS3sXgq)o`G5M;uB42Lwv*LYZCpfIdj7LVOf>H}F@k@273JjrQF0Rrm;lN0Xs2%`f6EG;5Z6>P>F$}4a)"
    "n48e9&Gm<5=31xld^3cg2Em3NbsnF2GUcf!>06}LALbksrRU$OgVls)EMnDLKNX5wNxdXZGZN`r+dP<UaMqX|p)-)MA))=z=|lM&"
    "rf=lr>EuXDrPWNNQ5DqTp(zCy3QuTNap>aYg7YUXo0N-FIYG@c4GJ`A(4tDq0j*VJvAM5;!-HR*WKUvUWn{>>EaR4J;!$r=8NCCA"
    "Cr5E9x1{!#wq7ueiOVkZZ{R7wnZf%x5~{&Bk}#OVIK=n~GZ{{}@|sJi-T8x?V>1}8;cU?8O)FJZ^yKo4l=@3mhk8ZMPc^T?V)g{J"
    "9XK%@xU?S1`WDIc=L%DBl5noTxr%L73iPP5<Z_9p1u+UzAIfGVo+nq`k@n5k0*WIT95}h+Niq_una{Y8IYLfhrZcqDFz_%QU`kEz"
    "5>-&KvQk<|&5`<9TJNZh{)uoBj4t#`xUg~kAg9Qs1HW34`fuN8-mrc{$D`zwW6^^mB)h9)yb^KBhsqO8)xX)CM}&Hx$sH3(EE+gI"
    "N;~=WANsr0#$5gVH|6w(%zRDi^N)cdqH-US2F6v<7Q<N|_ch$_@nurXqr{WaIl7(ck(9z#ig_t#r54H||JzjWBNc;|*a>-K@gdF6"
    "0zYjI4at;0Hrkm6aG1fPiB<xe9cfG=jXqDD#o^wDM-F2>G%7fL<8_G73BD6_2<aYhyw33@$6e0Ex#V$e&rY5vV}1`LHjz?6Dtl=h"
    "#2f#1sZm=F#$6o8acbhSiq|KpIPV?lwBVe}_uR))4J+A&whyjCLVL=TsOV6$MV$osU3%^~p6B6^!<WRLQmM$MBsW+5Rn%IA1LFnR"
    "7UU-|v|w1nf`!Euo(q)BQlUhJAr&t?N$|DKK}EKoH&z=vy^~v4{AI-JPvqN>&q3G0mWA^(UgCHuQ>;vt9911^bf`V0(MIC7l*&@B"
    "NTVzbM<yy(mu0z^bzfA1`j1b6i`I($SH!6eWi8MhYDWKNG3b%e){J)G_X=Xhhf(3QVPi8vl`^O9oXK(B=gH((sZ;If=RSqB@^nvj"
    "IoXvyDitXQJFY#&Dnx?%Hx5tq2&7(Ayqa6Pss5@>c*y9#(V3Ee{832YKYlMsmRZ=X;iLelg#{by6Ldn{TKJfAsxJAxD3o~*BXjQ?"
    "s{=d*s4A$ZOZg_12Xq<HqsPTLS5*0B$>Jz$BeFhyV|{?rDHTSvU(s<-uNH-APF=au=k}Wify5q?C`<Jz^@%h(vJ7RdXE8eXZydnZ"
    "KJr^1T_Vy_p=M9*13O8c+?7ZJf*?Wd0;ltw$#SO6y%G0K?tAPgx0y(F5SgisR)65TO0g|<2Gm(mze4>19piKy@~O+GGhcFiX^QiV"
    "8mJ|C%upeTWP&m+%Gw{P{&j2tFKaF>*=j{HHO-qfUi*~naBM8;wU&;)Ay>ebOU^c8HBK?$oP@9ep@aPyPOf+zaOudEywsv+>h(w;"
    "V6_IX3zrI-bF9~~S;J-noic7y81^U@lG-Q7BpoMQ>vOBf?Hy0gJe%_D%!h+C7Sbw6yZY%-jrWnqd7VsEcBQGRV0MBB2}zbDIgq*~"
    "Z6h*NFOfe%{tV^->JFR}lp9lPNgYFswbXR5(x+~I<>!%r@&j`foV{?Fz*9v|wMhHNH=fiybnw=t<AcsFDPWrRWfZNz=boU1Decwz"
    "do|d!v6jGfm8J!{d!(wv=Yn2E3RSVZ6Z;m~=~kl@oVz$V;%q?69ycpmqw3@P>nAjS-j$#C8WLGZ-5|Syybbvym=`D+P|88cgGw2y"
    "O{kxs-^0WL(<#h`*qx(oVDFBD89GT+19MuqUgKtnYB6_b+}+Td;kAjuf?|Ct#HqTcrix8lYK^JWrp|(#fSiz~YnpCoW75_kcSfE?"
    "UY|}LU5A{yv6*GF!-X~%>s(56DaUq+oe8h9d>im<%&!T*d`ZrwdzJ1@x_7Zp(oaYbd6%xLD}79uY|Oc?YRrI7NxsB6D09%^VEr-D"
    "-$+fNJcsI33lzjGwV`>0?hrN(j+gkXQ9e%j1etfL6v;}`{6X%Kj$1mF=sBX7&#Fvl^|+U3XC#@5*kf6(Wt|tdEZ#``ggi>}SVaBn"
    "OXf2e3uxp(t@xmYH<L0Is+`DbQ|m;73r%k1&B=RmX31e!;%7-bBz2JPM0!m*w&XOBVEE}y|NiF+nF81f85gkBz+n<63ETv@RWZlG"
    "a82<Gl{~7Q=^D~wLt&TRC5{`M_1LU&`N(!o(n-mlrIwV=MeM8e^3u<WyB2RRXG`t}d8x{9EMZDNR)4MiEK|Ep?T3`7(nv_h)Qadv"
    "$m&3MiM<6nc|6_l(Wlsg%2leBsgtDMmWDNQ9<+YbrbpW+xjQ;K95*>0a&p0iG9S~@H6$!b*ok^oI3fOkWCC(&Xue>Xf>j)63e43o"
    "$Z%}IR)PCZ9z0}|mW|Ha`rII_f$6}jf!RIY3#h>IYd}_o>T4PoX!fSLs+enZD04Z-jTWC0d`a-l<(I)>g2T4NTq&fa;ECGIoxAir"
    "ac6Q1#NUbkmLS@_3eNJT$@@SvDj^ofFfbsfz+;PpIXVU0Eb(-wM2@l*vKO3ObEeDLJ!c=B+i-r)ZG&G^ew`${lImJUM;WK(co#1#"
    "_b3Pky7a96Jm(PGfPH~;2faDo)^xp7aOLNkUwdu$RFMWt4%S}Om`PoO91XGuNi=hkFe5hl3BWBP^$!yUXk|51`{s{<zF9IYP3bQg"
    "6B|e=tZ2a~4wE)b@*K0c5%N04Zap%Js=}y0g;8s(h)^lO)QMKZ)=m$tdqgW~US}zN;@iwbgc4;e%v`bj!ul5HDcm;kIVF|up9Z~-"
    "JYVqq#fz*MiBAJnfb?1ssSjMYV1=+yf#`&<ImhB`yHcyl{-`bVe<sj{WdRjHy=*aDP{p9>ljeJ#lvx#1$GpVmVw@$TU~)p-oE#V8"
    "P31I>n(OOg8!|n3tT9u^_39(lAGhq_8i>=DvCaV>R69t)APtitnq#b&r4zlNM!1(gRuWhplB;}8ql@T29hgG5fu$3!eB9JHx8_cn"
    "J3Ud$wsXaci?8$cKic0o&*G*l&7HJDnZISFV#`^py2`;PKj9TB4X7sIID+FCjx#Kp*h=Hrp`}5W7u|34Y_O5wsm;^7)cU$@PmL-8"
    "p^51|9$b7(@f}hrL;D9^9lBNN@#boYJ5{NUzy5FZzXi3RRf4t+8&3%o5cjjt?Md*|oc7Kw#HtakD@0#W5ipnjSc0_#7YTxoOr^_t"
    "2Z9bfhLJ$0?tKjV6P)Cz=g{>{kIP4++_PM!L5j_m_+}(i?p>MSBTexgxgpo9?AVdQ|K{zM4_nPd^<OjUM1TC{_rO}gZm0PO62*6r"
    "E`rejGY88RdUd>4(JxUiMYk<I)0}?F{w|N#r-d3ue~q^%-UI4oXkpRKqi2>|6-i{3iT)(0ZexE%`8&NXvQ9)owR!SmC^li@aVpEH"
    "9G@oq&PpOK!AydAWTZwjj)Pyv^cd|YJ~w2X=zgK6N?A2-AEexuMmsW9?_^kTe__VNS%<1|8pJr3;zEMQQ}zn%z4?5XR98}a-C*=h"
    "Esuu=4>J-xB8eK((3D;nF@GuqY|&uAwkk;;d|1h#F0-`EE!no^@Q_pMW2b!ku!MI94>htq8tpi5abY8gj+iT{)gmi3Nv#yRHs$6t"
    "w7HPxg2DY65A#x3O1&$a=!Nyi{&OGJU2rSdUQo~GaYA<KNT<IUxnib@nF*zu(z<=5`g~*zyCIIFE;ImITW~RP&_ySK<224DI6I+d"
    "p*KQ5MQH_{?^N1QFUMxUrJ5w)lD<kKExnxhwmi;JNBY-TYb<WWs!KB_t>s6mo|o#P-NoJ<*IRtsR4<Y}q`{bm8JZ3_x8zEJtqymh"
    "Y2B)YX@aqa_5$`ba1HPixU_H+$D@k;1zNO8#s4tL^(psyJg@O0&D#ne7EyEkz^e8&4L+VTcoisiphAOYW11i6)T8@@+fUw>_|W6`"
    "O^mMeM-rG4yrWk7f5o_A>w&E&9;#GU1@DNSEqXa@Iv)!iqi0YlV!MRhKAr;%hSZDEpvr|7*A%cFO1>?%ot(~cpXf%Oa?K^o18@h}"
    "+u-hv`#5?Vd^%KAssBmc0`+>dm~iaKg&dbQT$^)i&R(9qwlq}6EJ#0)RZJdjd31E2r*G)4V6%ZBjnx%aeO!)km8Qv(PGdHD+^+I="
    "$=eklhB7b8t}a0|Gtnm^b;_wh-^n*fWjtG%#`s|Jy(GC%ifcJXj;()wv4PAA%pr_x;5RVc!0sM<OY|y~Tadk{{+w0@n?12~{L;Vt"
    "a@UH`xpJO`^$WH)*fk@Gnp_or$HNcv*Kf*ABT^NRg$;Y!`}HYP*NO<`Qq!)cRPIE#gl-uxPYO#Mgwlvd3cVI)1v3{a*qk%DH)f|M"
    "^@CRG55Csab7_!Ktp0x-s`rRcX{0t#l>U!3N;Xy3<iu(G<gQw|fy}qE+D2OaVfulKTlQ`duSS1tV-Ul~2vtlUEI(2eqn|r2y?AJ`"
    "Q;K9dykwy0!YPjJGoG4KGqj?jd~%149G)k1JaNk6d^VEn-Rfehi0K|08_d74QO1*t{)D=3Y1XB6k=9F2YVE^MgTH=DdZkHA0RcZE"
    "6pkELG*$op30Ba*!ool^gKm*>BWm=&{#3aJR1*60_n@bKhhZNUD|o!&efbsZj^AS3B@_Ed^pVY*Q(naB3{z`@f0lho+=F--@vd^o"
    "$fNwLS1Yn?!&U{EWF%QCgtUj&20Av_aoD}^n&x<d<5$jXx!q*H%E3sgH)-UhQIyU?Mkg5$<h6*}2g*xqO+_F3s5+*;iMX1=ThTK$"
    "E}9QC6+xil1FbGv7pxmJ4p{~NFvDT%qt`{U30sc~Rj$u@oMr{WrfQ?k<UWi{6r6b9F}EW#N#_a$FUdB<+ACdj4&E$dW{#CPom}38"
    ">;_VAM>@3|6DvpsSXkp&kN0yt-EC63sW_e2Dp#mhVA;cBK<6R_Cr<2m<Vj*8mLq*XGE)J0mc`+LV->En<yncOdR1Pzh*Zhp*5pi8"
    "TE1-c6zL+rbwu+mjZ(zwgNhb3ld$aK%%S>C;!Wv>VoyG5Rpt0==#Ih8%e)cE)Vx%$Sa8s)QQ=6V4BIN*xl&0=t)nfL-_*2tIph5;"
    "V%0Fy3oz0Vqqnt+L<fw=HLs&SCtsS>*T)_d-q^li5Kw=~=?5Dti5Zbr59Or_s{y|CEb5pYTSI>bhbP8Xc$!fmPsdPd1?knq9gBB~"
    "%=C6yekgs!*9XgnZVdKua2wz*&|Lign%*Nxc3?>m+rLxvIqEiC(1w3A-P6r)L(VF-)<7Z!kXmb`1`;VY<Uwdwu5Q7I$oI|4tlT|3"
    "B7q2(p@5c;gCVX1D)y=DlYQWLnTw{s%u#@6%7Fj;;h1U3TuJoaLMj0n53C+uE&L>D@#d7pnGe5DlF=%lOd`e_8c^xU>;k))Eb6i{"
    "Z~HXeG@$l^J9^xV!E;AnNd2a38&GxjZmS_>*JRp|x$ZS>uI5_I^qgE6fVCEPV^D2|k}$hLq&$+t`H!KhL@B-EUc}eM7uN?5_vo$+"
    "rhcM-!T1vsD@-1#-r-c8Q)f=kb+L-&pU2<V5Q_t}w%F2QW1{P3LPJUg3Tt?-IH{jf#l|o@OG68aighu*;8E(&e|${{<+HNv$h!3<"
    "=(}B1GPpFDl0!~DdDCUT$6;I+V_E0EC0Y#~vc?)SofNNvyQD&u4q5uFzGTIee_`6fXM&zH?q6k+l1bYXD&=ovv8y_yN&T52xj(<b"
    "hl^8<m^gD^rv89}P!Y8D1x<`KdE7Cm5?`x?mp6t<Zd43OFOE#1>SD2r=RGxTa-TdN^DO!nYngTosM<b{D6uE!NL`<XCwc62!AQln"
    "cgk(4cB9?cP}JCc&hTzyr9t@;`M#kl@m(Y-_aV1U?vy4qnnXMpa@gQ-BK_N!NndkOgNg%>KIr|2YM|oWPf9E$@0wxJXO)*wszO8M"
    "$PN|DJT6OL4~|yQ-`5HP8(SGXxm4ZrYs7C`(q-{-hC@rtRAukxnRFmH2zheh^N_E$*c<7rO@VsY&?0L?Zsv>Y3A7@-cIXvx(xuUp"
    "4tLHhxsYUQ%L7-gTM4V0R|Rv)2)YGWpWqnB`w(9hvMo}f>~72NfwW97ZM7WrIIf3Qn}FsN22<=S$JN3^n`&2bPSoAg>q74gr`}v>"
    "bGOXikx#ZH)q`H-X@*q?ZyOu7Yd$UbvXNmVBVWdKQNiwi{L5JEpnibf8b&cVR^jx7@Qj%{_I*5e@a~ay<6_9QGPjGoYVsi^`=p%A"
    "yPtlj8l&NbPEG!7B-4_e>1<{DR~hO~C3Q5YUT(6nT);|!Ck;(fM#Jq<tj;-yb1f;Z#d>JEvLVSPl*;(oQ=`wR16ML!U2%QHjgzDw"
    "($w;-L#gJP>npxp464tRl?|ju$ByBv&9&hd*T&t#C?W3A5R|YMluQ>nwdI=bZZjuQ4{#80vMb7h|Ht<{r*ntS<%{T>8h03*V_}Ko"
    "9KI`*3~8w4<a^A$ywITDi_wHa4mwZh9WWnaVMD_PJ$y-=#Zqz>8aCDTqK(Dp7tznx`2BdW%qxPPV(*RcT)^`aA0>R%rBRe&%M>Yt"
    "l-C-k_t8cf=(I3j$Hocm3))vX=pp&8__jJ6TN~`(fB0V4kTP-68A>5^ci`<o1#MP~oFcXQ)Nu@h4rJmuk7=aw1Jx^S18Ll4oHZ2n"
    "<10P5+8TOE7}(fRVebW(0s0xd&hepy*kk|5;ZvH+KmX(3{_(#lIX*fX((3_nA1^Q2B)+)b24h2P1;aXwLb#SNUBF2Wr$byUa680Z"
    "o`MS(MqKj#)==SbqW6XMosU&G@367OeMIXrZPN5<(<kJ^UixjrrOc1Ae*fcZd4Iz<<jYvuWA%zJ7hfLjd)$n3$Ci9c3Ogx{r7@Mp"
    "Q5t87?7#K&8qXHS9T-QjIl}V}&j&6KxSmix!}gt>zntocLi_i<gk&9@EoN*S&2V|eRgwA&8g;qc=cdZB4$^O!8Y&FTWnuot*a4GU"
    "EahmOq)mbL3px}fnUeBM?2}>Du9(tesy)dn(CSXBkk)(JWx3R3cg60O?<!+<rBE^a57j$qaJz6n;9-l$1fJ7W@Tr#M&X#*qp0rs-"
    "sIj`l8<H+c;U>?QuBq>htJJveeGxtLGWdPXAeo195>91I>@bzV^cnLi=DSe-O8FP<-sHdNIHgmZ3o3f_dD~%s&Y!r%?~-?=Q4uRG"
    "?UAhee;d|GsSC;y=bD&nVSkO&5-!U0F44QiX`8(X-%}ixI2=hSBbAm^+hRMS<h;55HlmkC;&5GKB8ANpuCg?0QCO$viSKtQZ^aG`"
    "qxOSlkj$DmfBVQ>vD3y`K#L-+RAjm0?m-eq>CdIV5WggTS<Y#>Ir26#r>7OqL+RJPh@QQ>Ld(T#8t)H!9yxL6-G%*&#3xd>{&FeX"
    "P#B|ffA0a$;$NbA?)w5$F@xDZbZ#l=yOS~(K%oc41r!5A({lTIKzfPAGxm}=d*gD2TV)*b7~ZH=pk0Z}N%ro%+Vf$^fpTea$tX#%"
    "#9GSeBIAW@bEYvRaODe>BN$|0)WO6C=XV<QX|$*DLt0H~H^q&~^(0}zR9O8`+dPMfNW2_)RZO@z*&3SlL(hk_I~p+ue+pYF?5J6x"
    "+k~P>k+r+)EMZoMXA(0joTSjV(QlzY!K01G7oI1Sh|{n{W0xiiu0MFc;ZMrc)e9^WtbVX@##8)D((bAN4<&&&nrvxbqnBaUTL!J)"
    "&j8Xl@YD-FVU~qc4K5Y9dg$b_)xwD~FGbuf@SY`=uGH4loYPq4xlNkIX_lo~j%E*<&*)gC+ni&{v(`9U=E9n5ac-X2y|CBkX^ig)"
    "u{{~bWLc4ARn|3G*JWE1U*%0@c{JozkXOw-n0kxi6k2QObTFGltAXnZK8~c6Xa1taoC_;1gxt5j+&X(NV=|3a1v?j957GA}n(4w&"
    "ML!YG@4Tq<Z6d9tw6!78%KjQdVT)c3{W1naYOJWElKc-%PTX`@1>eIbkH!+5<he3c)M&jY^j3yI51(;dNyw(49fx^@i5KR&=nZht"
    "z@dYqDUO~vIpO?+iydwsc&g&PfzJ%riab?XS}=|1lewqg_Z7D<9=&<Kmd0EbfvhXvV!a>b4T(3DKj0P6ALBj1AkALH&nCYj>D8sz"
    "kkd}FYxR8Pq4a>Vib`^Da^SRtTNVp%bWb#M+0C$9<MEmg6}}buw&cfwqnIo*vdGCQEvut!?((?EBa~=t+SJL%U1B4JXnz>?f1h!v"
    "9iVAJa{$K&oD$gU<77#dHr@7gdr-9JLY<E}zTQnO)x@C(hcS*fVq0I5GAhM3Z;HH~^WiSTq>K!+K1S-m@+Pm7yo-PS)0~~e($hdn"
    "H>xA-xAC~6dY^h9F6_ChZ1Gu&JF#nqY4^h_-ucgOrX4TA^N6!N?YCT<aU&AHBg&uL0?lEPYp+0em!ko{4$|Hma!oZ+;AWBA359>d"
    "_T7p17VlSv@b5nq_9<#=L{HnMZj?=O_aU3Zm!y1;HKqBPRufwJ+-@7PvSP~&jz+l3aoLqwLuNN!pc201jxt9BYX{Q*jds6L1i2*4"
    "3z%QhB~OnH*SkD=@%YN?GViDS+DLCFy`v0DGFZrDEwi<5Vx^_(@%xB=qc|v)UnJxj`xfZ6)iO<A8r%M0Yo-Ccacp1aL|!A`6J#p5"
    "Xk)H|!xYY&IN#&rMj4xmOS&#8YH-5i@`SBC_ukwu^RO-zTbianbl_Ue<G`kKn!|*Y8`7WX0=-pbhP^mm&iHsyFrcT)R*k1KIs1k|"
    "jmB+`uc<gyX&I6VZBq^CreU6d?G|<q{HWk2WarMqCeP+#-DR|uv3?H|b7J1sp!<W#&;a_1)G|&Ew6y8wa=EIgU#C{mGGM$?Mm?^@"
    "#l{y`@87+{qy&>G+HFHq@o(No>#dRR*8i3U^(Ox_9((*gN~-$jzy9r?`W)h1117KCx47XMOiAuw0;f3cPPjW$c0s)X4VyHs&}=}f"
    "1g%=^XC<DHL{hp7Q)l->(M*(hcdOD}YLdAqt{6!=FqlgArv?1v$=gwHOt&hRU)*_#>*!(~726qTm0?-O(FVtgDM@~ve4BzjJy-Nz"
    "uvOQrO5kOal8t2$7023R%*Uu!qxys{W2vYZBCg8Jg(7KARk*ffXC>o<sLbTQzNv6)PyU0?FTT2lr1t?$zK{+G7vQW7rn+eGBIN`B"
    "%j|({ZlRUM+X{mszGL`~Q(bjPomL~xpTs(7w(ZaFdj#nwv?k!XuxP;H0e98&9-doxYtt-7^A&Bzw4HIx<3xt%Etx!Jc9O+FR(IWq"
    "nXuUxc{$+qocB*YTqPElN+`pUj1w{q442Y{)=NYB2-F!`c^K7U+JXHCQyWZe@m9yj0beunOQdq*(||5RP9!;zX1m0T4KFXeb@-5H"
    "e<ZPi#8n8_)D7wgv!wuiP%2tt>4c4jLG{MD8FV5T^*OzlQBizL&Ph|IHz(ww8pp%+iz^Wy)eWeGvJzp<rssyU1z9#^d6#up&P83U"
    "C%z~9FQgARX_+!ktM~iaLS+w?8&m^m^q_r%sE4s0Hk#-hP=%9=I9*TlPzqXN_bm2Jf}5)T-5;6F!es&16@1p%d*i*&D$3kuOu6nn"
    "2cjaDJ*;{7IB~wsjT-lhVn1cllzBn(ZGOm}(^8qn60Ij(ta3ZcZA;QKQ>riE?!azC#S@p;Y{g`vpI%?Bb7|n~N^#}%0z=U^MfV}m"
    "rgNM^N9NX-s?#|OYa^QEWjr!8JzJE({01w!W6d4bkp|55U$F!xx|rV3M#V4z$Iqrj?}Z#d>y1_)t%&+%I@;{)xupeJ?~BCp0BdEO"
    "Z)jVjU0J%%KmYajBJ9aogZkp3jt2f+8O3}_L*E++f;0p(9>o4qwBg%-;q4FgMzl=RvhpP;f4277kAE@c)8=uk4bii4QJ`d;a%CDR"
    "L$ad%n;RFY55-CuUUOm29c=$Ert<x|&7*&zdLI0OLra{5v{wHjs-CNsU>6g&BcAuAZhp|eisKkenH65IdA;Ss#SrvWtg-T6@E>Yq"
    "SASu%-RXtPJmp?rlInEdRMLi|G6(w!Q4V)W@hV@cdc;lx-R&1sa*!W^lY;RGwkmJWVnq!^8jnqUZOQ3T*rey4GiM&`dD-Osny({C"
    "9b`R_qtc+>-)6M=DS+Prze~~aOVo+b4$3F6miRi+C8Ve<iK{6bDXTR5Li(cK2N%j*Y|7wfXv$_)%HXFkUB~nVvu~^?u%5#D7407G"
    "I(W-tkjH39(>LcTyxg%Gnd@8zZFzhjNw2G>o|vv+yNlBhgBC_7vb)sI&}+eoHzyOEuCSN<%dUDdzk<9E#U6MQxE9EDIqq=MjF363"
    "-sW71J6CqIUy=?ZVmL3*tS`R*m!`s!AVuMdA|sw=<C*gtw0<AO->CWfeEz=6Q20P`3@r~jTbOKMwu5ya_EQKF@T|hKj`1WGci2AQ"
    "AjH83hY|W!nm%Y6(QeN<rS2P^JG{v7YR+qmHzVG5`Osv4#>YILe7>Cd^^{mbVm*nkB%2duz_LZLax!_#v?nuP{I+?-opL#G7_?#3"
    "fW3N-0UT^N%(3{!)(txe3_VIjG&s|&%9S%WZ0^Rm*W_KB4;g+(G8xEhB6~+p-$B(TKK+7f3+z2iUU2weB}c^-l_yjVsI{W@oNil+"
    "zT>U@z&H($8^%@IUB;%5%>cbUdI#(ca9GD>h^HN@F!NoejYFF?rx!f3_*mi3x8p;-vcKh=$IXchGctM@vI=fSZ&)dH^C>eR<vK17"
    "UyUjl-3=jM!CnmaDtqWqx=*bI4HI-}(c{RK3ab~hR<fJRp&+N_x0+JGolA$6q~BkH&X9+YTf?9Vi!v-)a6H0mgtZ0sRM>OHS3v0("
    "RSUFj)BDDmh;w5u47j-A(T49`Y1G6i{B2C{ZJR>k4$TaV8@SI>=E{pcFD_pOy??2K<1r-{lzLLOLCZ3!&^e!CH;{d3xU3(Tjj7b9"
    "=9W~7cy93Z+rFjEc>whY<}X-0U^&G5k+KD<rpZ3j@J^RAMQN_6q4oH&5#_@D$KR?-N6J~4_}I>3e?<8c6-#6f$ez)*!|@XrpVH~e"
    "b8YxlfLLB~uKY#xG+qK$H`oTSAHiXTryK?j#fmgo@J7XEiNB0WgS<F-U4!ZX><E4U$1(&t2sY@v@S4Z)gP$f9_oRZQddOCpJ4fy<"
    "d9~t0OR_h~=ft1N$vnF8n$-&0M%>JC|2f2}P_<#v!$Je=X><eZtMuuN%RcT4xIg0|#Cx5b3OQAJC_B^O>Wl3yKLdVSx*<LHl8C<0"
    "fBz9DDXq11n$qe0rRfb)3+$Bm+~;fcOVq2hbpxtY@AZM|2^K{-R&XAt!I+OtS-7$?4{F@wK8N`t=c_U|&Ov8y$37gl*sY`6q1TC>"
    "1Gnz%#pI}R^0GXK@*I6x^<2gN&;R+I*Tl;13#!b@d(g?F^NsIkL(@x<Dt?V`G0c<gk?m8nNX<Gmhm!k9yCs*6+%sQZy)|fr;~s6N"
    "Y<1aN@n<ZXjhyE4{<0|hUEj#m{IpSz=?d@B7t<TqA}rNuwlD-GseyyB7S}vJJ^6AmR9A&l6Dc&MHj>&^thlC{MB>xKeTvf=&e_tN"
    "f61y(w=?uCS~ocr=XFAnzcW`a$AJ3%+%dQl@Y+}y;9_IQDiBCN*p1k0@^me+n4#`|WT+z6my9igt9ZbEW6{DvkM>JLI;)pxL2ro*"
    "2Uj&b9r1Lc;e_uMNspwX(*2Yyda_)~YA>s&>|+vK<yqF1O;`|?sdO-y^86LksPg*1VfOc}BB(YN9ZqdHb>wABmzjwyCuu;HIj27c"
    "Qdz`&29FS@JDfFfH5OmLt~vGA$J!Db5zZqHsvH(2K9<%@T2JYFhDYB`xW-r<(?vY`c=kE*l5}4ht|`{vUJ0R{!O9qS6-w-AeEt?F"
    "^-OLk+u_^55cJ&^`XS9utEvyZJr=fTjj(^j^??#c&OW%PqPmdp8A<puKFXpk`<k3;rkXygZ9&h4iLxyowmax%aUH|gj#@s?lf23D"
    "uFHOo{hcHaVsF3I^evJ#7$#uU#@ZSuLtM7;QKkBh&JJf>?zgyqmdr$qsF_Hkq0DL%M@bw%alOaO9`9|s9k`Z~SW-GSd74*I0{p4N"
    "r-A7n<wLPvnz%CeO*P4R(y>OtnA2}=O?h45ZAzBPo0?xyX=u!Yd=k1<=-P11gG!+iB}^*U7NPsW%K&dp+NuP*!zzD@nIY4wX-}-y"
    "Xj!0WNzW{o>l~&eQ<jlRo!YV-%Hb~Om|Q)%wIs}$TIM9r4&olH?x29^j=_K{F4u4DS>kDR{GMNn2bdmE%M7ma&UDL5tf~>So*wmn"
    "Ujs<)p;iJb39l)-IULn+HOJSJ`pPdnX*1)}Q&jR)NQ>pk$vn5-qVyQT^nyxPhdWvOnyRNEDv-88pN+a|ioSW~4DK5|2S&<24&mLv"
    "TY}3^t`7MAk(e#%i{aF}cMhD5@v$qDmCTlM(t2r9I#sy@vk>-4d_<HV^6ev5!Ib{vhm#cp>BC`VjD=V)pgqNCMe#PZH>4cIY0a1Q"
    "mq|%ap$hF09;X-{=<RcC#$i{ii!@t?MTyIEjA^9;RlK<v+4z~#)ul+qnVt-`GHYr+<)QL6jt?A+#jYEQSrSe|Z3~Y$L}wfw@ED_H"
    "lI#b^FC5q6^v(2IS8z|bEK00vXnN209pZtMwed6`YY1rmp+%C`aYNBIy$|&|cHauuE@)&^c}L}koGXT+-+vCD8XgWjZb;+xrRsUk"
    "HY8>c)bU>8$3vP$Q}XXWysWUXY{n;9{P1OC%YI=fT6#(k94AS+zkF84rk2U+%4r<kXxE_Yk)nYi|MN#0+Rtb|RjYwFkVe^{%INv8"
    "KmSv2tF^M~zp_%CesLzrV~=NRL(w+LF_6y5vrsO>T*XjRPB*!>{TApOl~eF(W9xyQt7$5iSSAgiYpLj>M9Di<R<upfV@aP47g92;"
    "%jorG(mFGO*MllWP0%aw!Q=N;hVlmLXK)hW=P~W0x5i*ci3cgg$(^XABB?4TlWcdnH{g-WqdspF>{}dFWO|id)ikHiKUT4J#p8_{"
    "4{7bcR6Sj}C%ehbP!sezzI_0<1?~mBGI%SvZXu{baL42TM`=8*@HWI(nI?6*?I^nNM@8~YX&q%yl)+r~L(`D{f@d6^EG05j8PKIj"
    "kC5Yi?iQpKNxLNOLG$UES_{ex2vb-cp)<l$5`!6y)|?n{b|$a8yi1x>Yi|q+BPe#D_<>#@Mq^k#;4p>j5)%W=6)-==&J=qkO4TU8"
    "qSl-X7hX?UiQK0t#hkRq;=aXO$kLIuE4P(|o^D(jg4!8s7pT3Vo(J2;))2=FJQVSIpwx(}0T(<jO}M#cFDKWUgfV&QSI`0b@`NP^"
    "FHchWvD-03)sapGcWZ;_eAa{K0bUgh-xQy)_u*sdTc-E)uJLF%_2PQG0_7tt<8Uv-dr#>l1!=BTxPE3QE3un&RScOj-1?hFCk$5j"
    "*ic&~I|=dk-vX_~Bl^VUcG3h_Uk(@kJ_o<gHN^7}e?hVj3mcXe_-{;QaeTwef)XB;W>ncz>qGYgJ+_?Bb1}>HIXh=Qd}J_^!CHnx"
    "*{|hfo2GT1QGr@S%QLO2v|7=n@GbanKWu4GPe{ggSnA<zgu$HFJzB50a^c#Qy%zTZ&7;HL64X6tb-~-hu?Y7K9=muP;pIcgD(#=V"
    "p73ESiLEr2s){*3J0)vwVAXRfO8cK*OS8(u-o<1QOE2tnu`|QY3a2ruDQyX<>(IDGH=m+6r`lYN@zeU&RNeND!8-}>GA7&D&(rNh"
    "uR6VKeivm|)?AGr%+)oZ_et8n(e?LP{(bMEoP&A;<}27-;i2606k|!u?6I7{-JJ3&RPW1mBw_x~|MhSGFt=^Bd6LqIzCkM}Ld}A1"
    "7Q8!5t2lm8??|uow^+SV<pcXQoZ6UCI<NAJh$=o+-{h6(-lbqdj}N^^^j@-x9g9V-EZECPrzV}5bnfE$G9AhMU+w96!79}1(A~rR"
    "jw>bLa~`P7btJ>IjKVLQo+=5Uae`(5?E~z3*v#Q@MB6$i9K$mD`F#(6KSL-@;5>r!3%gkybf{RN;*N?T)tfYO_?6<fBk`J)J?VJj"
    "^(7j9>+5@h16W>QQ-Vzd&pF;I)KXqCOPvZ`lbpNr$ma8yFH<Ra;?%`yNb4kFR>JUa8~W_|kY+Rb*t}9=I{(tu<6ow@f8jktyMm!D"
    ")sS7Gtn#)x94&~Jn9bqX$9Wm&J3OcG9N}e!!G>aaicKi1dU{Kj6J4uxpHdWOXUxu&2O$ruJdJqUV}Hz#z9bUn2Ubddn^5$jT!pfY"
    "r2&@SSbJdOj<$!6M~@smx15Y~D$S`ZZx-y=B(au$QHCSagqg0M)(mX+GGq$ke&s~jz*ZVg8+=@-mZe3ERKy;RD4OutkwPfdnb<ka"
    "s;m<AE8KZ=Z_fTh{N|UcCx<$aEI>Mkl^AwI^gA@m(D6xOfgTP$rd&}8SSV><3a(glv6r&n%gOuOfZl;Kg31`81cq&L1KJ<yndCx?"
    ">m8n@Bzc!qUg{@l-elg9_2OGmjfQ1`wTJEm+yv~e*xzCQienXc=J1uJ*nmn|8mVk%L;i*$WxialEx5VkjwP|O6z*cj#7oJlEbF-J"
    "a&qZQus6+HYG7TQ*|^J6>BPMX56h-NrEHlFWL9w4X#DR7s#1{7LiPrw1e9_ZAK)>>%Z-zP;nA1&)_*_a-_ITz5j<w}?sICzr?kv#"
    "vI%8>k>H_Ozl%su@P}~QWBX0nkcvGjC25!Dmc{KZcei|3Ib&TqE9pF>yOvQ?Mu)$(wb$Q(rL4_a1|OL$Wtx{+OJ;XD-sIH(vgz!+"
    "iX98Padh*vJJP}Ek3K5xy%h9MWobY9ze%ZQb3AV_I8kCl-ipQ{UB?u4dDM`6UaY({2ZmSgBus2E8(^!5&lOctR0*k(rB>3EFMm)e"
    "?<mGwoBg^WDf!;bVHe=}Mym@4X}O!<H$E<vg<2Y%1?(qStzb7r=>pYLTs!jS$-6%L4o3}st;Fw|N-E3F7ua#7cQLs8!!H#XtmT-K"
    "vzFLzikbANGoj8_VtI+{Au`|IQ9~DCu8O%6z6Rt*G<eaxK<9`qK7~qvG89cYrv!2;?V@x$GF!=ZWvc7_R3mUzyp)z~{7cf2r~?ZN"
    "!T@6nOhnkLaxTZ^ELZfi=zSC65S$0B&k*czouW^JtqhNMJUj7x#0&K+zIYqM{NI11W`805smnU_i|`uLsKVtLtL!N;mPT7T+8b!S"
    "E;>+bLA3*u2v#3>R53oqW*M6*q}<^}seFdgbGq*7)}?!c9wmy_6zw=ZU@Il*mh2{S+?&?)cefsJf5AJ8`#c_9ZsvGd;HAa;9q&~z"
    "l$KaQ;-0Lu9GZ`y)(oKc(jMUufnS8P^1vf_uQ8s$))p^UyxLTqa5={1C9lSO7)xp;^NSp-a%{^<3zg10k`AO*&K7{JGMNabO_+6I"
    "n}>TAf)d6pj2|$O$3zp89%ee2KVsX)P9HlycFyR|v0uaG61R6e*6=#Sdl~}^A8Se;DBYw?iZUI_gp_|!Ax4EE6&BR((5OMn7_9<Y"
    "2XyMw`$V56Cj!p&IQQo2jJ+B6(>y5h?8Ckaz?PD1N#-D>jo2eO)FnvB^KRZke;4BcpCo)#R3D+^W4npl3GOmnIIy$k<(S{D)K$%r"
    "%$MIf+Ok|Q`M~yo(nTt*I6aefS@u;`uCu0E9NKN@SfGqvu7o?A+#`h%CuiL0a>wOKmi@kXF&XA$6v^l*+pejrcg7T<=tA)X#WM^#"
    "=(?1tQZ}OUf!0fQHthJ4(N;;H6LCCn%W#~)yMz4`u1<JqQRP7mr6UWj=eQoQ<%^|crfaI`#L1dUrlB!qR)#A&E1@~^KH$SjGW9?I"
    "w@#AkG4=nSN+-jG225zu4`A(LY>BB4R^C)^aZy>jv5eY=#asxGgiHo11E{ybQi`_&F9Ti!JPZ007;Ryr!v72QwzzuYrjMsIh9`W~"
    "DCbjYNhKAHPAG`ev&~M6`#B!R`IY8(Q=UchBYJ<J1E(|GUYJN@Vvb%8mv4%NRGHCfN~bH|3VcuUqb&Kps1(RKigS{7T7r(ehq@JG"
    "#4=O7Uoj|P&?Ik6`!RbJIp5^sib|z}vtd+*>oq{vM>T$fBVAJTnCk+wvHs&pd?>|-BIwBO(fxgRzmGmF(^ws%zoKECmp$I!I4bgM"
    "!5_<T>L9NKl^rx*V8@|5gP{-OChRZpn!#(1ohn8HO4*e1DRrc5in2`_c4>6r{F(DN&fj_S;C+exF8gOb+I%VUwI}J9xN(`5WogN("
    "CF_!$H*(3zJ^B4%y$-7EO$Uc}T(ogH!ROnQ{QD2O9%VlqQ#ya(x{~}Yw`M$#OWl{oOzsH@`<hh+g3mEjEEv|{l!HqXE)L!fl<!cj"
    "K)o&(UflI~)G^FDBc4I!49z2SFECocybOx~PAc7Oz`X=_2cjFUYk2xlqC#$shBoc4<lnil;Yydi1}|g0ABq!6Ya{K7jH)uSW$a1#"
    "`u>JK9CCnn9`jqQcCi&;w~f6e&I+iq(L{`r1xjzJ>X9>|cA4G<cH7cEOXnqi-L#?nZo-3p7lsE|bYXo2e*yju&PuO#Sbbp4!FrZ*"
    "5mgsdpHlrs>pHCu6lOVb;q8#OBlai!dPuw@@tG95GI3<um2*x1fc}<67pgZHE@A3n$3{1Wt}=mFoUL)TLzR6bRYYIpdY-p&_8XFT"
    "NN*;+r3~XTy2{l^+3E+YH_F{9_h6MET3Ye6j|>yPVgC0q|9yOZU)|r=8&WCA<)C0e-2yiUg8*htIEHXe!7~BR96Xhm3gMH%*as60"
    "OjR*+!|VqObu5MG#IbE*Pf5xG*Ja$V@sP%I2Cp-GjPbRi%#gBOYWn1;jI>1^W#I}G6etWSs?c*x?-jj2^eND%M4vN#o}5Ur{>-$>"
    "TGH%Pxz%Iu!u=tSt~{}M;_~v$s|l}F1aaV<&-*<e>U@~6Uts0hq9?y@5(^|zmx?dVhBS{dtH~lQD_2$@*)QcVk$c?qi9X!qW5(g+"
    "luO+&Ntx~S728AXR%o=<H09}|HKbxt@}MyX?*P*~ES9kM;I@U^9bP?*jd0k;Q-Yj2waVmmXc3X0<fP9vi&qUkH2C!5OT?e4#9I=7"
    "$Z#s-noKS-yO>|mlV$_x4`Dim)fNtExD4>Vq2hy=T}kz&5EJVr-ula}p3}L(s0iB&&O(e-hi53}Qg%S~6}dSIa$K^xyx>a6wJFzT"
    "T${5sW4Fw`EcZU#AM-HHmnvV!{Hf?xb+SSB(t!GV7WLnE9jZB~p21Za=M2Ip4!Sg}areUOHXn|X&CApDlG!lkC^2lv*yy#s8^Rxh"
    "KSk$*voW4R3<4^IRJu{=PWF+U2`9E(@wgIj*XQ1ZpErpYB(ay|Rjiooi*o$vw)DMaHE>qoL@*e@d<~lx94t7m;kAJG2oo7h<uLum"
    "_7S%|+@A1Y;q`*|1HP)1R%UBR#UqtfOq-`pgGMKs&*^yL_=giQPMo+<WNX6BJ3Ddq_B?6wI?tOO?_TUT`M8mIR^p)~<I?cOc}qJd"
    "!<vj!GTF#%BlDfCcCtOnHj;ZFVMapJEjs+#H8K9e)Q~buZeG}lal62un#7Bev?Se=OkV~Y8C+!$={gVPCMv(r<nOEV`{_e^4f821"
    "U05Dqd4y{Pt}dqXnCfCRh0YU49h{ADp26)D0~>=LKI2q8Q>8<7kD5Dj6EuI&dP|!Zr!Sm+b1u%g4A%ng^tos8qQV=O{TK&6KR*(C"
    "OQJ2^w74sowq+iZO;UDUIh^G9l>5*;q~2Xv|9$qL7(pq9?HHa?v>VdyNf(vb?l{)wLLkY76f@$UR2@A%9&Dgegqj210{T0cSXfXm"
    "FvMw9N>!<K<&u}nQi8bzJ5xm;!<oQk4mStmAujiL4>8<RQaQgor8{&`nc9xiCC+xZ8c1p^PAKjCx3a$UBoEU)?vr>v(qbX~v|Lo<"
    "2UD)UGL!+wg6R(C4ulU@iEctiHRe9s3RWEKmT~H9imOcM05UhI+0Y)tz>}=j8nc@!_WLaSzI#xLpz(pu1|~<CgjjWO8{%_Kr54R9"
    "Jeu%m%I~{mHewwm8kp+(yi6V3Aq+dPh%jYiT1D!2RGM?ia_P$D4cj}h2GYJr$CttRTTP#=nL_sls|&0h*!u9W;p1U8kBt<LJX{`e"
    "6H(ozMujY!9u_^9^h(gH#2IB^k6d|hloPKbm#y3mqVoKpE8$#1{l0ph&KiJy2j^r^<3lHq*ugjtlQcX6Okc4$Ln}_L9CduoCMD67"
    "R9l*^xMi8ya!kv)V`?jV@@zwD4fQ#MFZigOYKWNx+JP?K{NQ~`?HFA$^ceB`$)ALwtE9Bk#BL7#6Fm#8I^?Q*O|L0fb+8?1_NnEF"
    "n>Iw9$t@wCgwzw#4wPO{nZQhWvn*Ctxc=bNrkYRQlU_^iwRu#Q<Xd)G*|jAa$us=6qF0-INJr=|C~=`ygH!-JEto>RK(~d<8+&J3"
    "=QtOWWm6aEP0^j-Xa=z^+)tR#V9UX2ng$b^HfZZ`Zo>H~7e`zib2rX|h?S71tJ28IsAXEv9^eFi2+;+eO=`cXv*)fQ-I{cV(ml%L"
    "B(wdORSo{@NbL`;Dv}(_@FL@~j2&IrFrQScLoo}DDh#q1|6n?SzDm>2cq?JJAbZPh$g3d-1r9E9XqbA+V3#LQEuxpFyhHCgSB8?#"
    "i`A5<r#bX(r<LDF?)T~bev~9!z)OLbhkX`~AGkf?@y1z<>M?59Xdcq~Oz#)xvb^r|VJ9hNP?qxA$~$jb)2Tol_YEq>Y0}_nUph<a"
    "HvjxDeQ^o66$R*Th1Ar4y`#3#(tw%MuxAF`D&g3o7sKI}UTbdlC2dP4Z^}+eSUre$!LCD3g`s_TEHGB4S(lR?wmQ;&8ou)nwwE|9"
    "P;E>*Uz4=W;fE*nmoy?xFw+i?1U;_?Qwdf3VxWfljT+qN3NZ^qnKF1we&+f0G9-OwJAvH|Sq<JNdEe!;%a;OQ`%-#Kb@^q|7n!#~"
    ")k;-4YjJkK^{pvU?`%(?v&Qs{;y239sGFcok~TTs4fqk4*iiG0l>KO6FG0gV+`71)A?mr*qk;7Iv_De+XheTyAcgIufz@m79QOBL"
    "M7#bvUvhjMiftHHDvjzuJ`2qSv>gZ{EcCIsLMw}>F`i%ep3-(t`#CAkk-P9|%a;jXJ^q*`SO56ovr^fo987m?)mXMLKEa@j&n@Kx"
    "@@C}CIlI=SCiZZ4bb3)Rr{GABt060)oK>+}#+i8=eUDQH$_=<QuvO*TS*l5C_EnKydMWjwk$@nE#T#Arob_aPHbwf3ZXSm(imSgj"
    "B^|Qgra+CXzov8Pi|AV<5}aMIedUYA*Q9J3rsUs$=sb}4P9tVLW!!>sA2TY7J<#hdsf{ji)quye*y@RobO?<lwED34z|Mo+8Lk_="
    "r!XqgX-xMlJ>y(D@xtTfn>Ty*+k86lSsAz(UsHUm^V8#ZMM@p9kFwdx!Io1b@2qP7?-ivB*tc-fq?<=7@J)9(SaUd)dR(?i!}ZS}"
    "oTxOg%7;)Ap`;p;3Z=7^)k98N*7X+L#lQgi!kZ2F1zf)9r9z#n92;`hW!ly@iU!bM54U0O!{udgt%?n7_xN`F;{WYOR#yY+duLYI"
    "pWvj0?-7MQw<7LNb(vD2S{CX)Eb_1@;Ml^*qQ#E38#=z|vf-Gm*~||6_!eUkCMR^SQeb{mnJK>Nhd1BFtWfSrGl#Azic-9C`H+@&"
    "WJ>kzqc(IB*y#NEU;oFy{qH~2i)r9HPtn|g8^eZAhYX(|eC^7tBy;;;4C<#Rdzc(Bm&HaOpBF0JsQ9<OlH}eH#vUdw)V$N}LP3$9"
    "F?u%mlICk$N|P_+SxKt}%@kT=bT)V`;I)sRIDQTke^TL0^DeDNT#EDIDe02bPSQNcbz^Gl6NLp!Hm|>!KB)UISm(?Z_zCcHWLIeD"
    "(4;G4_g|%Y0XhTo9z0Yau)<}WP75x#S)U<J^Q8CX+x+n7C8e6!GwFu1whY;<&uy{T!Ezo)CA_Jmdu>Y0b;<YDUszd>Xv{BV?6#Vs"
    "CpW?}G^SX4<DiejG|rcl2&k3f?vYnDKIFc9N*7}fNL=Btr1q7D^DkMS*R!EK#9SM5bIkcT%;9K&lLdw@Z8~&|D0*=__{*-h^X7h|"
    "7TlD^*C04U=wi>I_Jxx%PKA6{=4dWm@5`^OU^)ZUE%aOHmQV#}n*=4MlwMH(P1^#!R-DN4%I1U0h^~AyLZeR|c{{Q%wC{>F5~nS#"
    "?*r(aMEkS>mp?dY<F&(fi$5KSb>wbIxcHXo<9}x?9nfo1{mAD!UvpB~No6n1thBGvnTVH_OGbj#-x~VUUoo^lxSLQb%gZNk%Dl_-"
    "L5ay!l66V#45zZs)(M;p7TQ>UU}sDdn@(kReIBHEyJ3ILN+e%pey8~TeINaS)*Do8SdQS_fk6Fvpu*Z$8$K=g?6K6v${p(mv`%Q9"
    "vGc&85`zkETs%b>^)Wh7qC?FowQOolXtAQD65<BkBYN#QGvVx+vln)EJbCfr!0Qqp13s_$lH$vfFE73yIoR^UVl|v9rc>rKnHH-n"
    "oq=>u;^t-DmU~BnyLn=je!o)?n?mdfg9{8)?!CtR0_!`RmT~Wpd!g%{<0X#Y`R;IV;Bd%~y5v))zWM5Xboza^sF)$|&7+KD%ikg$"
    "l-?mzhx!^C4cK?E{lrd>>?C!M)P2xVMWhLOOeo58GR5V+q${!-$a-oT&^hk>z)`@jiy?%n!Oib0`}?XwVTC=FFCX!&^x#g8Ps0t("
    "dbF$4KA=OM{1#oV^oZy)<6?y?S+2L(jqx(#(}6F0DW0XC5-X5JO;(ZlfyWOeRg%A>wu%En+B`US=3$W^X^GV|hjxRt23Tis&X})Y"
    "HNe3X2NBK^7)<1q`ZDQL0Bv-N=v3&GkXP|bRtCM5#Nh=GBlL54dQ(55Q=3;I@02`UC6N}dF6W7ahrczHv~E0j4&ix)myg{OE(iEL"
    "Q@KWsB5j5|Py#!VV@4kRZxyAA%{a`DSah+ylSaXiRl=n_gi?k0{)_8ttmhEFLa;z*fTJ<aUA*R~TBd4+>=b!@@&+_q(0EO=1I;Tm"
    "cWJNGzs1>ztvH|heCbO(FWH`ypVG+5@%p!Iz0{V6>;l#voM&(u!#fV|3%suwv#~Tml|1KmRP?FTq0WqYaq4-r>(Rc-jevul<mXcA"
    "O5-HfUJj0F)>SX2yjPm1H@@z~Zhfh$Q~OP7y5xkk3`x25af_D^J`_0G@GJPs_1~0zKVW5mO(RP>uMCS&-b3vO_bb{5?DudO!?BIi"
    "86M*_n9;07%MOJ$MOjXaIoab>f(t$NRQevt<)j<>W^Ta_K0_Q7$W7BVL6J&b4%~n7=q}~A)Y{S*i2eBTE2*w8;oir~iQE;PW}F-F"
    "p(L@RDOV5nuA+Cu;Tji1T+MK^z)J^T7mCfPw5IBiOGkEHUZ;5z@IiUyfR9^AyrtlXHU74uLa%j<6C2*7*w1L1KH5`ElDDMcK=ziL"
    "3Sa6!6ldxS>YaB@xa{DTgj))0C3IHkY{{*X+vn$7CbhqeTI=&zQ4Va0Pvuwd()@gj^fA6So}QF5e$9MiVhvSg0yfZ#!8C!TEVh=|"
    "KjUtUr!LL%bm`C|;KH46Z+=}ReUK<LmA~fz$1v)G@6)18%RMJPoZfJ7{W7TD47#*$aH4GpI*Ypf1`)K6;MTF2LRFmJQ-4K+F0Xp>"
    "$bI?rLHP`19eg-{jN7bcpjCoR15++`7w9^aJ5v2l!;r>hnszw<mTp^m`9CJaIR#kCV<*CH3r9IBD9w0CD=YhfM2A1$ZL%uMGQKf!"
    "g5DYWSr|WHHiOv$P94k^@T~&uf~>ti=4PnWALI9$F)SacQ=)F2y4^qDO&8@YwiAE8-(b8$)WD`gnJtwY)X352$nhih54^ogd?{5&"
    "{F9tZ^347*CuvepPeLPv&I<Y~7#3i0!}tvoYfO$X)x`W0Yayq1Tr6<y$;~crrtI4sx%}Emyeg@L<knKQ#lDO4ly+6xOBq#U+?A;%"
    "vyb>SIj7{-m9X{4oV=;W)&KugW}x)@Qu%g`PiMaPfAlGuPYT>UEN5_Da%Ii#o0l%{L$Ot=uCnZ&T-9G+!t@`L<g6QfxGC^Ie<1aN"
    "bPduCyi_?>l2cQH;E%bLH>Ubdg@e}okFwBWH>Lgg^UX_QU{%1*L+cEKB$k%A4aC-k`U1{8Jgzv(qrc?pg*RvRJJK&pSo&j3FDLf0"
    "KKwB@H5>lXMs)dsRSb?DYQ6uMO=0mBH(q~?&6ED@&;R49=k!(ZXyfHbxi?j396R%8C5fz94Vji?cK!4JrqnPV{^(%PQ9<<hk4YxB"
    "3*6OkuiQfhPgAlEWS4k*=KV!36$xg4%x4A<H5%;MesU`mZzsLfpYP1f1MDmGuP``ZwJ+B7kC{zbuVMYfVvENqo;iPvzQk`(Z$jJV"
    "*qmF<KmT9wQ5I?P{{vC-mdX"
)

_SIBILANT_INITIALS = ("zh", "ch", "sh", "z", "c", "s")
_INITIAL_GROUPS: Tuple[FrozenSet[str], ...] = (
    frozenset({"zh", "ch", "sh", "z", "c", "s"}),
    frozenset({"n", "l"}),
    frozenset({"l", "r"}),
    frozenset({"f", "h"}),
)

# Curated Mandarin / SenseVoiceSmall int8 acoustic confusion pairs (used ONLY with strict >=3/4 exact guards)
_RAW_CONFUSION_PAIRS: Tuple[Tuple[str, Iterable[str]], ...] = (
    ("ya", ("a", "ai", "ye", "yao", "yan", "wa", "ia", "jia", "xia")),
    ("yi", ("yu", "ei", "er", "yin", "ying", "i", "ji", "qi", "xi", "ni", "li")),
    ("ni", ("li", "mi", "yi", "bi", "di")),
    ("hao", ("ao", "hou", "hua", "he", "ha")),
    ("xiao", ("shao", "qiao", "jiao", "yao")),
    ("zhi", ("zi", "shi", "chi", "ji", "ci", "si", "yi")),
    ("hei", ("fei", "hai", "hui", "wei")),
)


def _build_symmetric_confusions() -> Dict[str, FrozenSet[str]]:
    adj: Dict[str, set[str]] = {}
    for src, targets in _RAW_CONFUSION_PAIRS:
        adj.setdefault(src, set()).add(src)
        for dst in targets:
            adj.setdefault(src, set()).add(dst)
            adj.setdefault(dst, set()).add(src)
            adj.setdefault(dst, set()).add(dst)
    return {k: frozenset(v) for k, v in adj.items()}


_CONFUSION_MAP: Dict[str, FrozenSet[str]] = _build_symmetric_confusions()


def is_cjk_char(ch: str) -> bool:
    """Return True if `ch` is a CJK Unified Ideograph (`U+4E00..U+9FA5`)."""
    return bool(ch) and len(ch) == 1 and 0x4E00 <= ord(ch) <= 0x9FA5


def _ensure_table() -> Dict[str, FrozenSet[str]]:
    global _CHAR_TO_PINYINS
    tbl = _CHAR_TO_PINYINS
    if tbl is not None:
        return tbl
    with _LOAD_LOCK:
        if _CHAR_TO_PINYINS is not None:
            return _CHAR_TO_PINYINS
        raw = zlib.decompress(base64.b85decode(_PINYIN_TABLE_B85.encode("ascii"))).decode("utf-8")
        temp: Dict[str, set[str]] = {}
        for line in raw.splitlines():
            if ":" not in line:
                continue
            syl, chars_str = line.split(":", 1)
            for ch in chars_str:
                temp.setdefault(ch, set()).add(syl)
        built = {ch: frozenset(syls) for ch, syls in temp.items()}
        _CHAR_TO_PINYINS = built
        return built


def _ensure_ppinyin_table() -> Dict[str, Tuple[str, ...]]:
    global _CHAR_TO_PPINYIN
    tbl = _CHAR_TO_PPINYIN
    if tbl is not None:
        return tbl
    with _LOAD_LOCK:
        if _CHAR_TO_PPINYIN is not None:
            return _CHAR_TO_PPINYIN
        raw = zlib.decompress(base64.b85decode(_PPINYIN_TABLE_B85.encode("ascii"))).decode("utf-8")
        built: Dict[str, Tuple[str, ...]] = {}
        for line in raw.splitlines():
            if ":" not in line:
                continue
            pp_str, chars_str = line.split(":", 1)
            toks = tuple(t for t in pp_str.split(" ") if t)
            if not toks:
                continue
            for ch in chars_str:
                if ch not in built:
                    built[ch] = toks
        _CHAR_TO_PPINYIN = built
        return built


def get_char_pinyins(ch: str) -> FrozenSet[str]:
    """Return all tone-less Mandarin pinyin syllables for a single CJK char (empty if unknown/non-CJK)."""
    if not is_cjk_char(ch):
        return frozenset()
    return _ensure_table().get(ch, frozenset())


def get_char_ppinyin(ch: str) -> Tuple[str, ...]:
    """Return primary Sherpa-ONNX `ppinyin` initial/toned-final tokens for a single CJK char."""
    if not is_cjk_char(ch):
        return ()
    return _ensure_ppinyin_table().get(ch, ())


def text_to_ppinyin_tokens(
    text: str,
    valid_tokens: Optional[AbstractSet[str]] = None,
) -> List[str]:
    """Convert a Chinese wake-word phrase into validated Sherpa-ONNX `ppinyin` tokens (empty if invalid)."""
    clean = str(text or "").strip()
    if not clean:
        return []
    out: List[str] = []
    cjk_count = 0
    for ch in clean:
        if ch.isspace() or ch in ",.?!，。？！、·-_~'\"()（）":
            continue
        if is_cjk_char(ch):
            toks = get_char_ppinyin(ch)
            if not toks:
                return []
            if valid_tokens is not None and any(t not in valid_tokens for t in toks):
                return []
            out.extend(toks)
            cjk_count += 1
        elif ch.isascii() and ch.isalpha():
            up = ch.upper()
            if valid_tokens is not None and up not in valid_tokens:
                return []
            out.append(up)
        else:
            return []
    # Require >=2 Chinese characters to avoid trivial 1-syllable KWS false triggers
    if cjk_count < 2 or not out:
        return []
    return out


def build_kws_keywords(
    wakeup_words: Iterable[str],
    valid_tokens: Optional[AbstractSet[str]] = None,
    *,
    boosting_score: Optional[float] = None,
    threshold: Optional[float] = None,
) -> List[str]:
    """Compile a deduplicated list of Sherpa-ONNX KWS keyword lines (`tok1 tok2 ... @label`)."""
    lines: List[str] = []
    seen_toks: set[str] = set()
    suffix_parts: List[str] = []
    if boosting_score is not None:
        suffix_parts.append(f":{float(boosting_score):.2f}")
    if threshold is not None:
        suffix_parts.append(f"#{float(threshold):.2f}")
    for raw in wakeup_words or ():
        label = str(raw or "").strip()
        if not label:
            continue
        toks = text_to_ppinyin_tokens(label, valid_tokens=valid_tokens)
        if not toks:
            continue
        tok_key = " ".join(toks)
        if tok_key in seen_toks:
            continue
        seen_toks.add(tok_key)
        safe_label = label.replace("/", " ").replace("@", "").strip()
        parts = [tok_key, *suffix_parts, f"@{safe_label}"]
        lines.append(" ".join(parts))
    return lines


def _split_initial_final(syl: str) -> Tuple[str, str]:
    for init in ("zh", "ch", "sh"):
        if syl.startswith(init):
            return init, syl[2:]
    if syl and syl[0] in "bpmfdtnlgkhjqxzcsrwy":
        return syl[0], syl[1:]
    return "", syl


def _canon_nasal_final(final: str) -> str:
    if final.endswith("ang"):
        return final[:-1]
    if final.endswith("eng"):
        return final[:-1]
    if final.endswith("ing"):
        return final[:-1]
    return final


def _canon_sibilant_nasal(syl: str) -> str:
    init, fin = _split_initial_final(syl)
    if init == "zh":
        init = "z"
    elif init == "ch":
        init = "c"
    elif init == "sh":
        init = "s"
    return init + _canon_nasal_final(fin)


def syllables_fuzzy_close(s1: str, s2: str) -> bool:
    """Return True if two Mandarin tone-less syllables are exact or acoustically confusable in ASR."""
    if not s1 or not s2:
        return False
    if s1 == s2:
        return True
    if _canon_sibilant_nasal(s1) == _canon_sibilant_nasal(s2):
        return True
    conf = _CONFUSION_MAP.get(s1)
    if conf is not None and s2 in conf:
        return True
    i1, f1 = _split_initial_final(s1)
    i2, f2 = _split_initial_final(s2)
    if f1 and _canon_nasal_final(f1) == _canon_nasal_final(f2):
        for grp in _INITIAL_GROUPS:
            if i1 in grp and i2 in grp:
                return True
    return False


def syllable_sets_exact_match(a: FrozenSet[str], b: FrozenSet[str]) -> bool:
    return bool(a and b and (a & b))


def syllable_sets_fuzzy_match(a: FrozenSet[str], b: FrozenSet[str]) -> bool:
    if not a or not b:
        return False
    if a & b:
        return True
    for s1 in a:
        for s2 in b:
            if syllables_fuzzy_close(s1, s2):
                return True
    return False
