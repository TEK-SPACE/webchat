﻿using System;
using System.Collections.Generic;
using System.Linq;
using System.Web;
using System.Web.Mvc;
using Recaptcha;
using webchat.Models;
using System.Diagnostics;
using ServiceStack.Redis;
using System.Web.Security;

namespace webchat.Controllers
{
    public class IndexController : Controller
    {
        //
        // GET: /Index/

        public ActionResult Index(){
            if(Session["nick"] != null) {
                return RedirectToAction("Index", "Chat");
            }

            return View();
        }

        [HttpPost]
        [ValidateAntiForgeryToken]
        [RecaptchaControlMvc.CaptchaValidator]
        public ActionResult Index(IndexModel indexModel, bool captchaValid, string captchaErrorMessage) {
            if(!captchaValid) {
                ModelState.AddModelError("captcha", Resources.Strings.CaptchaError);
            }
            else if(ModelState.IsValid) {
                try {
                    indexModel.Store();
                    indexModel.Rooms.NotifyJoin();
                }
                catch(RedisException) {
                    ModelState.AddModelError("general", Resources.Strings.DatabaseError);

                    return View(indexModel);
                }
                
                Session["nick"] = indexModel.Nick;
                
                return RedirectToAction("Index", "Chat");
            }

            return View(indexModel);
        }
    }
}
