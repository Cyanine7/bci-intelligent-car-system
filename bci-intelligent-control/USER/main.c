/***********************************************
¹«Ë¾£ºÂÖÈ¤¿Æ¼¼£¨¶«İ¸£©ÓĞÏŞ¹«Ë¾
Æ·ÅÆ£ºWHEELTEC
¹ÙÍø£ºwheeltec.net
ÌÔ±¦µêÆÌ£ºshop114407458.taobao.com 
ËÙÂôÍ¨: https://minibalance.aliexpress.com/store/4455017
°æ±¾£ºV1.0
ĞŞ¸ÄÊ±¼ä£º2022-9-26

Company: WHEELTEC Co.Ltd
Brand: WHEELTEC
Website: wheeltec.net
Taobao shop: shop114407458.taobao.com 
Aliexpress: https://minibalance.aliexpress.com/store/4455017
Version: V1.0
Update£ºº2022-6-20

All rights reserved
***********************************************/
#include "system.h"

//Task priority    //ÈÎÎñÓÅÏÈ¼¶
#define START_TASK_PRIO	1

//Task stack size //ÈÎÎñ¶ÑÕ»´óĞ¡	
#define START_STK_SIZE 	256  

//Task handle     //ÈÎÎñ¾ä±ú
TaskHandle_t StartTask_Handler;

//Task function   //ÈÎÎñº¯Êı
void start_task(void *pvParameters);

//Main function //Ö÷º¯Êı
int main(void)
{ 
  systemInit(); //Hardware initialization //Ó²¼ş³õÊ¼»¯
	
	//Create the start task //´´½¨¿ªÊ¼ÈÎÎñ
	xTaskCreate((TaskFunction_t )start_task,            //Task function   //ÈÎÎñº¯Êı
							(const char*    )"start_task",          //Task name       //ÈÎÎñÃû³Æ
							(uint16_t       )START_STK_SIZE,        //Task stack size //ÈÎÎñ¶ÑÕ»´óĞ¡
							(void*          )NULL,                  //Arguments passed to the task function //´«µİ¸øÈÎÎñº¯ÊıµÄ²ÎÊı
							(UBaseType_t    )START_TASK_PRIO,       //Task priority   //ÈÎÎñÓÅÏÈ¼¶
							(TaskHandle_t*  )&StartTask_Handler);   //Task handle     //ÈÎÎñ¾ä±ú    					
	vTaskStartScheduler();  //Enables task scheduling //¿ªÆôÈÎÎñµ÷¶È	
}
 
//Start task task function //¿ªÊ¼ÈÎÎñÈÎÎñº¯Êı
void start_task(void *pvParameters)
{
    taskENTER_CRITICAL(); //Enter the critical area //½øÈëÁÙ½çÇø
	
    //Create the task //´´½¨ÈÎÎñ
	  xTaskCreate(Balance_task,  "Balance_task",  BALANCE_STK_SIZE,  NULL, BALANCE_TASK_PRIO,  NULL);	//Vehicle motion control task //Ğ¡³µÔË¶¯¿ØÖÆÈÎÎñ
    xTaskCreate(show_task,     "show_task",     SHOW_STK_SIZE,     NULL, SHOW_TASK_PRIO,     NULL); //The OLED display displays tasks //OLEDÏÔÊ¾ÆÁÏÔÊ¾ÈÎÎñ
    xTaskCreate(led_task,      "led_task",      LED_STK_SIZE,      NULL, LED_TASK_PRIO,      NULL);	//LED light flashing task //LEDµÆÉÁË¸ÈÎÎñ
    vTaskDelete(StartTask_Handler); //Delete the start task //É¾³ı¿ªÊ¼ÈÎÎñ

    taskEXIT_CRITICAL();            //Exit the critical section//ÍË³öÁÙ½çÇø
}






