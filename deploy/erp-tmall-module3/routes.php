<?php

// 在旧ERP routes/web.php 中 require 本文件；使用既有CI会话认证，不公开匿名查询。
Route::get('workbench/tmall/module3-inspect', 'TmallModule3ReadController@inspect')
    ->middleware(['session.auth', 'throttle:30,1']);
